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

### Connection, data and trading presentation

- With both broker links ready and reconciliation complete, compare a known old update before a farm-restored notification against an equally old update after that notification. Both Connection boxes must stay **Connected**, and both Data boxes must report the applicable subscription type and **Stale** age. The pending-event reason must remain available in the first tooltip.
- With only closed-RTH and stale/pending data blockers, verify **BUY blocked: RTH closed** or **SELL blocked: RTH closed** and the complete tooltip. Add another risk/recovery/connectivity/worker/storage fault and verify its warning remains prominent.
- Verify delayed and frozen modes remain identified; missing, invalid or future actual-update timestamps must not become a fresh display through a cached read.
- Verify **Last market update** remains the actual event receipt time while **Cached snapshot checked** advances on reads. A new actual update should advance the former; rereading populated cached fields must not. Check stale/pending cached-price wording and unavailable-price wording separately.
- Observe the next valid update during an active session and verify the Data presentation recovers only with the existing controller evidence. Check that GUI rendering itself sends no command and does not change snapshot data or button permissions.
- Check Simple, Advanced and Debug, light/dark themes and 100/125/150/200% Windows scaling. Confirm ribbon text wraps, tooltips remain available and long actual-update/source text remains readable.

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
- Verify the entire Live strategy bottom bar is hidden, including all five workflow buttons and the view-mode selector, with no empty reserved strip. The global lock must remain reachable.
- Verify tab navigation, history, flowchart, monitoring, and Reconciliation remain viewable.
- Unlock and verify the bottom bar reappears, its view mode is preserved, and each workflow button returns to its state-dependent enablement rather than all becoming blindly enabled. Repeat across all GUI modes, other tabs and theme changes.
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

Using controlled saved-checkpoint fixtures, verify that an otherwise valid estimate at exactly 24 elapsed UTC hours is accepted and one just beyond that age is rejected. Check next-day reuse within the limit and normal warmup after a weekend or a longer holiday gap. Verify that profile/contract/configuration mismatches, future observations and invalid RTH boundaries remain rejected.

Turn ATR adaptation off during open RTH and verify the observed bar count/readiness continues to advance while Initial drop, BUY rebound, Minimum profit, SELL trail, and protective settings are not rewritten. Restart the application and verify the live observation buffer begins empty again; a validated same-contract persisted ATR estimate may be restored separately as a starting estimate. It does not restore old ticks as fresh market data.

## 8. BUY order paths

In paper mode with controlled settings:

- positive BUY trail: verify action, type `TRAIL`, trailing percent, stop, quantity, `GTC`, `outsideRth=False`, app order reference, and optional account behavior in TWS/Gateway;
- zero BUY trail: verify the drop condition produces a market BUY;
- slippage buffer: verify quantity is lower/equal compared with unbuffered sizing while the transmitted order type is unchanged;
- partial fill: verify that the original marketable BUY remains working beyond three seconds and through changed entry/data guards, completes normally without a replacement order, and stays in Stage 2 until terminal; explicit Stop/close and configured pre-close cancellation must still work, with all fills racing cancellation reconciled before Stage 3;
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

- verify the Connection indicator changes to **Waiting for IBKR** and code 1100/2110 appears in diagnostics;
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
- Run through multiple fills and confirm queued backups are eventually created and `latest_restore_validation.json` reports success. Confirm immediate order/fill persistence is present before the queue drains; the backup is a later snapshot and may still hold up the single controller worker while copying/validating.
- With a disposable unstamped legacy database, verify a pre-migration copy is created and successful opening records `PRAGMA user_version = 1`. Reopen it and verify no new startup copy. With separate future-stamped and unreadable-stamp fixtures, verify opening is rejected without backing up or changing the database.
- Populate a disposable backup directory above 20 files. Verify startup preserves them, failed backup validation preserves them, and a successful fully validated backup trims to the newest 20 when deletion succeeds.
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
- In Simple, Advanced and Debug, confirm Price data monitor contains the Market and strategy graph directly below Strategy progress and above all five metric boxes. Confirm there is no separate graph area, duplicate graph or “Rolling graph buffer…” footer. In Advanced/Debug, confirm the order is connection/strategy configuration, Price data monitor, then Market and strategy state. Simple must hide the configuration row and show Price data monitor first. Resize the app and switch themes and view modes; verify that the graph remains visible, keeps its existing minimum height, retains history and continues to update. With no ticker name available, verify the top Ticker box reads N/A; with a populated ticker, verify its identity formatting is retained.
- Check full and filtered Strategy flowchart views with current and completed cycles: all stage cards must use a common text-fitting height without reducing fonts or overlapping text/arrows. Repeat at narrow/wide widths and 100%, 150% and 200% scaling.
- Confirm the five monitor cards align, Data mode includes a continuously refreshed age, and RTH status includes UTC and system time. Expand raw fields and stage details independently; resize and repeat show/hide to verify raw Value columns keep filling the width; hiding either must not change strategy progress or permit a blocked action.
- In unlocked Simple mode, confirm its view-mode-specific visibility change hides the Live Strategy Recovery / audit log panel. Advanced/Debug and the Reconciliation tab must remain accessible and existing audit events must still be recorded. The mode guidance must end with “Stage details are expandable.” In both themes, the three numbered Reconciliation step headings must use regular font weight.
- Verify Live strategy, Strategy flowchart and Trade history remain on the left and the Reconciliation selector is on the right. Click and keyboard-focus the selector, switch back through all three left tabs, and verify the selected state follows the page. Repeat while locked, after theme changes, and at narrow widths. Existing programmatic navigation to Reconciliation must open the same page.
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

## v5.1.0 targeted integration checks

Use isolated paper-broker instances and the actual Windows/Python 3.14 build. Keep the full `run_all_tests.bat` output and exact installed versions.

- Connected idle Live-to-Paper profile edit: status retains the established session; Start is blocked until explicit reconnect succeeds. Check failed reconnect and unchanged-profile controls.
- Recovery: foreign/contradictory execution identity is refused; exact-owned legacy evidence still recovers; a waiting-entry cycle with a known working order pauses without another submission.
- Manual close: pending BUY cancellation retains supervision; late BUY fills determine final size; partially filled protective/final SELL cancellation closes only the remaining quantity; a full original fill requires no replacement. Repeat across restart and cancellation failure. Closed RTH must retain an existing exit.
- Leave manual-handling confirmation open while a cycle/order/fill changes: acknowledgement must be refused in GUI and worker. Ordinary price changes must not invalidate it.
- Controlled quote and persistence timing: unconsumed quotes survive later size events; cached rereads never become confirmations; final BUY transmission obeys freshness/session limits after slow local work.
- ATR period/bar-size edit: old readiness cannot allow entry. Unchanged configuration and a valid saved seed still work.
- Cross-UTC completion/late fee: original completion date and loss ordering stay fixed. Test authoritative negative/zero corrections and excluded fee currencies without changing quantity.
- Lock-button geometry: same top/height as all ten status boxes at 100%, 150% and 200% scaling, both themes, narrow/wide window and locked/unlocked states. The native automated geometry test complements a visual check on Windows.

These are qualification cases, not instructions to provoke failures in a live account. The current source release does not claim these native/broker checks ran on the development host.


## Completed trade summary and history filters

- Use a database with completed cycles for at least two tickers, then configure a different strategy ticker with no completed trades. Clear all Trade history filters and verify the summary includes both historical tickers. Changing only the strategy ticker must not change those totals.
- Select one history ticker and verify its count, commissions and net totals; clear it and verify the combined totals return. Repeat with date, outcome, ATR and Paper/live filters and combinations. Confirm no-match filters and tables containing only noncompleted rows have no completed-trade totals.
- Use more than 500 matching completed cycles. Verify the table displays 500 matching rows while the summary includes all matching completed cycles. Use a filter whose matches are older than 500 unrelated rows and verify those matches can still be displayed.
- Change filters rapidly while a worker response is delayed. Verify a result for the earlier selection does not overwrite the current selection's rows or summary. Repeat after Refresh and after switching away from and back to Trade history.
- Complete a cycle in a controlled test and verify its row and resulting totals refresh without pressing Refresh. Update its commission and confirm the displayed net amount changes. Check numeric sorting, audit opening from sorted/filtered rows, and CSV export using the same filter combination.

## Targeted GUI corrections in 5.6.0

Use deterministic fixtures or a paper account for these checks. Keep native rendering checks at 100%, 150% and 200% scaling separate from headless logic tests.

| Finding | Manual verification |
|---|---|
| G01 | Disable one ATR adaptation option, enter a manual percentage, and deliver a queued snapshot from before the edit. Verify both the visible value and saved configuration retain the manual input. Repeat for the global ATR toggle and the other per-field adaptation controls. |
| G02 | Select Reconciliation's market-sell action. Verify the potential-loss confirmation defaults to Cancel, cancellation issues no SELL request, and explicit confirmation still requires a current valid probe. |
| G03 | Compare a local unsold quantity of 100 with broker quantities of 0, 1, 99, 100 and 101. Quantities below 100 must show the shortage; unrelated excess holdings must not become app-owned. |
| G04 | Present a partially filled Submitted/PendingCancel order, then a terminal fill/cancellation. Verify the order stays working until terminal and cancellation controls follow the current broker evidence. |
| G05 | After a flat completed/stopped cycle, confirm a different instrument and receive its first tick. Verify the ribbon, monitor and graph use one instrument and contain no previous-ticker price series. |
| G06 | Use a cycle with a large pre-order high/low and start a trailing order later. Verify only prices inside that order's lifetime affect its estimated stop; repeat for a replacement order and restart/missing-time evidence. |
| G07 | Select a historical cycle, refresh after a newer cycle appears, and reorder/filter the rows. Verify the same cycle ID remains selected while available and a missing selection is handled explicitly. |
| G08 | Select a completed cycle whose ATR, repeat, reinvestment and guard settings differ from the current draft. Verify its saved settings remain visible after changing the draft and after another live snapshot. |
| G09 | With an inactive cycle, protective SELL enabled and protective ATR adaptation off, verify the manual protective percentage is editable as a next-cycle draft when unlocked. Verify it does not alter the inactive cycle or its broker orders; active-cycle and manual-lock restrictions must still hold. |
| G10 | Compare CSV exports against ticker-substring, date, outcome, ATR and Paper/live filter combinations. Export more than 500 matching cycles and verify all matches appear with the retained column schema. |
| G11 | Complete an ordinary SELL after cancelling an unfilled protective order. Verify normal-exit classification; compare with a cycle that has actual protective fills. |
| G12 | Complete an order in several fills at different prices. Verify the Timeline completion marker uses the weighted average while each individual execution keeps its own price. |
| G13 | Open audit data containing legitimate zero-valued percentages. Verify Summary displays zero rather than another fallback value or N/A. |
| G14 | Open an audit with broker-style orderType and placement/fill timestamps. Verify available type and duration are shown; missing evidence must not be invented. |
| G15 | Complete a cycle and update its commission without using Refresh. Verify rows, summary and flowchart choices update together; an unchanged history must not trigger repeated full-table loads. |

Open About > Info and verify all six support address labels and exact strings match README. Check copying the long ADA/NIGHT strings, window resizing, keyboard navigation and both themes.

## Gateway outage recovery in 5.6.1

Use a paper account and retain the pre-test database and audit evidence. Test both a waiting-entry cycle and a settled holding cycle, with the broker's orders and positions independently visible.

- During an active cycle, verify amber **Reconnecting** for temporary local loss, amber **Waiting for IBKR** for upstream loss, and amber **Reconciling** while the broker state is being refreshed. Affected workflow cards must show **Waiting** and Trading must show its paused status. Actual manual-review and trading-risk faults must remain red.
- Interrupt Gateway connectivity during recovery reads. Verify the saved waiting stage is retained, trading stays paused, and a failed or incomplete request is not presented as proof of no orders or executions. Let connectivity and managed accounts become available and confirm the existing retry path completes reconciliation before fresh data can advance the strategy.
- With sanitized offline fixtures for the two supported legacy holds, require the exact prior waiting-stage audit and settled ledger, refresh broker facts, and explicitly select **Reconcile and resume**. Verify the recorded restoration and absence of duplicate BUY/SELL submission.
- Repeat with missing audit history, changed account/contract, unknown submission, a working order, pending exit/protection or insufficient exact position. Each must remain blocked. Do not manufacture these states by editing a live database or clearing its manual-review flags.
- Interrupt a broker read after explicitly selecting **Reconcile and resume**. Verify that retries retain the request only for that same cycle. Finishing the attempt, operator Disconnect or an application restart must end that intention; a restart must not automatically release the old hold.
- Check that a disconnected or failed refresh disables broker-dependent resolution actions, and that a normal cold restart still requires explicit Start/resume. Verify the existing RTH, fresh-quote and ATR gates after recovery.

Record native Windows, supported CPython 3.14 and actual broker checks separately from deterministic offline tests. See [the release note](legacy/V5_6_1_OUTAGE_RECOVERY.md#verification-boundaries) and the root implementation report.

## Completed-order execution recovery in 5.6.2

Use a disposable test database and deterministic broker doubles for fault cases; broker integration checks belong in a controlled paper environment.

- Recover an exact final SELL executed during disconnection when the returned order object is stale or reports `Filled` with zero summary quantity. Confirm ledger quantity, fill price, completion timestamp and Stage 5 without another order.
- Repeat recovery and deliver the same execution/callback again. Confirm no duplicate shares, cost/proceeds or fees.
- Exercise a partial fill, multiple executions, final and protective SELL ownership, and BUY recovery. Preserve the existing working-remainder and oversell checks.
- Deliver commission evidence before and after execution; consume validated pending fees only with their matching execution, including authoritative zero corrections. Verify currency and idempotency rules. Retain valid broker fill timestamps rather than assigning the later recovery time.
- Reject a wrong order reference, account, contract, side, contradictory quantity or unresolved execution evidence. Test the legacy missing-reference fallback only with proven recorded permanent identity; an order ID alone is insufficient. Keep the new completed-order counter normalization stricter, requiring matching complete attached identities and quantity. Status `Filled`, zero position and empty open orders alone must not invent a fill or a replacement order.
- Interrupt a required broker read and reconnect again. Retain 5.6.1 waiting/retry behavior and require the normal post-recovery quote evidence.
- Check that the cycle audit and Trade history refresh after a proven complete exit, and that an unresolved mismatch is not reported as fully reconciled.

Use the [5.6.2 release note](legacy/V5_6_2_COMPLETED_ORDER_RECOVERY.md#verification-boundaries) and root implementation report for executed checks; this checklist is not evidence that native Windows or live broker checks ran.

### Windows/Python 3.14 paper-account acceptance gate

**This procedure remains an outstanding acceptance gate, not an executed result of the host tests.** Keep its evidence separate from deterministic regression results. Paper execution demonstrates that environment's integration; it cannot establish live-market execution quality or guarantee every production response shape.

1. Use a separate writable portable folder, a fresh test database and a dedicated API client ID. Select the IBKR **paper** session and independently verify its account identity in TWS/Gateway before any order is created. Do not point the test at a live account or copy a live database into the test. Record Windows, standard GIL-enabled CPython 3.14.x, dependency, Gateway/TWS and application versions.
2. Run `run_all_tests.bat` from the 5.7.0 source. Preserve its pytest/coverage, Ruff, Pyright and simulation outputs. A missing tool, skipped required gate or failed check is not a pass. Build the Windows executable through the documented build script and record which tests use source versus the executable.
3. Run a normal connected control first, with Auto-repeat disabled to keep the completed cycle observable. Let the normal strategy create a small valid paper BUY and its final native trailing SELL. Verify the connected SELL completes once, with matching execution IDs, quantities, prices, commissions and Stage 5 in the audit/history. Retain the broker execution record and audit export.
4. For the outage case, use another paper cycle created by the normal strategy, again with Auto-repeat disabled. Wait until Stage 4 and verify in TWS/Gateway that the exact app-owned native SELL is accepted and working, with the expected account, contract, quantity, order reference, permanent ID, GTC and RTH setting. Record the local BUY quantity and pre-outage ledger. Do not create a manual order with an app reference to simulate this setup.
5. Use the application's **Disconnect** action while leaving that native order working; confirm in TWS/Gateway that it was not cancelled. Allow the original native order to execute while the app API remains disconnected. Do not replace it with a manual SELL. If no execution occurs, this case is **not exercised**, not passed. Save the actual execution IDs, quantity, prices, timestamps and commissions from the broker. A separate repetition may include a controlled restart of the paper Gateway while the accepted order remains at IBKR; record that as a distinct scenario.
6. Reconnect the same app instance and resume through the normal **Start strategy** action when required. Also repeat the scenario with an application exit using **Exit app and resume/recover later**, followed by restart, Connect and explicit Start. If a recovery mismatch is presented, record it, obtain a current Reconciliation probe and use **Reconcile and resume** rather than editing the database or marking the cycle handled.
7. For a complete SELL, require all of these results: each broker execution is represented once in the local ledger; the recorded sold quantity equals the app-owned BUY quantity; actual broker fill timestamps are retained; Stage 5 and Trade history show the completed result; fees agree once authoritative commission reports have arrived; no second SELL or replacement/cancellation was submitted merely to repair the local record. Do not mark the fee comparison passed until authoritative commission reports arrive; a cached zero fee alone is not proof that the broker charged zero.
8. Repeat Refresh/recovery and restart once more. Confirm that quantities, proceeds, fees and cycle count are unchanged, and no extra SELL was submitted. Retain the after-recovery audit and broker order/execution history so the comparison can be reproduced.
9. Exercise a partial native fill only if the paper environment can produce and confirm it. A working remainder must remain supervised; a terminal partial SELL with no confirmed working exit must remain incomplete and require review under the existing rules. Do not treat a full fill as partial or use a manual live trade to force this test. If the paper simulator cannot produce the case, mark that integration case **not exercised** and retain its deterministic partial-fill regression as a separate result.

Record each case as **passed**, **failed** or **not exercised**, with the exact source/build version and evidence location. A pass requires the complete expected result, including absence of an extra SELL. Keep account identifiers and raw private exports out of public release artifacts.

The deterministic companion test must also retain the production-shaped response that triggered this defect: a completed order with status `Filled`, zero summary filled/remaining counters, and an attached exact execution carrying the actual quantity and price. A double that always sets a positive summary filled counter cannot exercise this failure. Verify adapter normalization, controller recovery, persistent ledger, commission arrival and stage/history result together; testing each layer's happy path separately is insufficient for this case.

## Partial-BUY completion in 5.7.0

This is an outstanding integration procedure, not an executed host-test result. Use only a separate paper account/database and the supported Windows/Python 3.14 build. Record the account mode, application version and exact order/execution evidence.

- If the paper broker produces a genuine partial `MKT` BUY or a triggered native `TRAIL` BUY, verify that its remainder stays on the original order beyond three seconds and that no automatic entry/data-guard cancellation is sent. Do not treat a full fill as a partial fill; mark this case **not exercised** if the paper simulator cannot produce it.
- Compare the actual final executions and commissions with the local ledger. A complete order must enter Stage 3 using the complete quantity and weighted average; a broker-terminal partial must use only its acquired quantity without a replacement top-up BUY.
- In separate reproducible partial-order cases, exercise explicit Stop/close and the configured pre-close BUY cutoff. Confirm cancellation is still requested under the existing rules, Stage 2 remains supervised until terminal and fills racing cancellation are included.
- Reconnect or restart with a working partial only when its exact broker state is available. Resume through normal reconciliation and confirm there is no cancellation based solely on elapsed first-fill age, no duplicate BUY and no duplicate execution ledger rows.

Keep these paper results separate from deterministic replay evidence. Completion and prices in a paper simulator cannot guarantee live fills.
