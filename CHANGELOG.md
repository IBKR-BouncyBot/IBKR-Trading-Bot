# Changelog

## v3.9.0

### Audit diagnostic coalescing

- Replaced cadence-level repetition for persistent Stage-3 quote-evidence, reconnect, BUY-preflight, close-before-RTH, and native trailing-order wait conditions with stable condition keys and reason codes. Changing ages, prices, timestamps, or retry counters no longer bypass suppression.
- Recorded one operational condition-entry event when relevant, one bounded persistence summary after the configured delay, additional summaries at a bounded cadence, and one recovery event when the condition clears. Structured raw evidence retains reason counts, occurrence counts, suppressed observations, duration, latest context, and observed maximum metrics.
- Kept the Stage-3 quote guard visible continuously in the Price Data Monitor, including state, stable reason code, executable bid, current trigger, distance from the trigger, current detail, observation count, and suppressed-row count.
- Kept ordinary cached/non-price ticker callbacks and a valid executable bid below the Stage-3 profit trigger out of the persistent WARN stream. An invalid quote near/above the trigger, invalidation after the first confirmation, unavailable field tracking/upstream evidence, and final pre-submit revalidation failures remain immediately visible.
- Changed normal unchanged native BUY/SELL trailing-order waits to INFO summaries no more often than every 15 minutes. Diagnostic selected-price or raw-Last stop crossings still produce immediate operator-visible events and periodic anomaly summaries.
- Aggregated repeated reconnect attempts into one outage condition with an immediate outage warning, one-minute and five-minute persistence summaries, and one recovery event. Individual reconnect attempts remain active operational behavior but no longer create one audit row each.
- Coalesced repeated BUY preflight and close-before-RTH/session-timing messages by stable category while preserving immediate safety-critical errors and every underlying guard evaluation.

### Safety and compatibility

- No strategy formula, market-data validity rule, Stage-3 two-observation requirement, order construction, broker mutation, RTH decision, ATR input, position ownership rule, fill reconciliation rule, risk threshold, or watchdog recovery gate changed.
- Critical order rejections, terminal partials, quantity mismatches, storage/worker failures, broker/local reconciliation uncertainty, failed durable submissions, and `ERROR`/`MANUAL_REVIEW` transitions remain unthrottled.
- v3.9.0 adds no SQLite table, column, index, migration, or persisted setting. Existing v3.8.0 databases and portable state remain compatible. Coalescer state is intentionally in-memory; after a process restart a still-active condition may produce a new entry event and then begins a new bounded summary interval.

### Verification and documentation

- Added focused regressions for changing-age Stage-3 messages, a 705-observation NBIS-style stale-ask sequence, GUI-only non-price callbacks, immediate near-trigger invalid evidence, native-order summary/anomaly cadence, reconnect aggregation/recovery, BUY-preflight condition recovery, and the Price Data Monitor guard display.
- Final validation passed 1,211/1,211 pytest cases, all 126 test modules in fresh processes, 33 focused v3.9.0 tests, 78.1% combined statement/branch coverage, 1,024/1,024 callable-entry checks, 17/17 safety mutants, and 58/58 deterministic simulations across 54 CSV paths.
- Added [`docs/V3_9_0_AUDIT_DIAGNOSTIC_COALESCING.md`](docs/V3_9_0_AUDIT_DIAGNOSTIC_COALESCING.md), archived the v3.8.0 release note/report, and updated application, build, documentation, and test metadata to v3.9.0.

## v3.8.0

### BUY partial-fill settlement

- Changed Stage 2 so the first positive fill of a triggered marketable BUY no longer causes an immediate cancellation request. Native BUY `TRAIL` orders that have triggered and zero-trail `MKT` BUY orders are allowed a fixed 3.0-second grace period to finish an ordinary multi-execution fill.
- Measured the grace from the first persisted positive fill (`buy_filled_at`), not from the latest partial execution, so a slow stream of additional fills cannot extend the working order indefinitely. The timestamp survives reconnects, watchdog replacement, and application restarts.
- Requested cancellation of the still-working remainder once the order remains nonterminal beyond the grace period. The original order stays in Stage 2 until IBKR reports a terminal state, and all additional fills received before or during the cancellation race continue to update quantity, average price, commission, and execution history.
- Added immediate remainder cancellation when an enabled market/session safety boundary becomes unsafe: RTH closure, loss of required live-data mode, stale market data, unavailable session timing or the configured pre-close BUY cutoff, the configured volatility ceiling, configured minimum-price/previous-close-gap limits, or an unavailable/crossed/excessive bid/ask spread.
- Preserved one-shot cancellation semantics through the existing `buy_remainder_cancel_requested` flag. A failed broker cancellation remains retryable; a successful request is audited as `BUY_REMAINDER_CANCEL_REQUESTED` with structured timeout/safety evidence.

### Verification and compatibility

- Added focused regressions for native trailing and market BUYs, full completion during the grace period, timeout cancellation that is not reset by later fills, session and market-data safety cancellation, minimum-price/gap/spread limits, cancellation retry/deduplication, persisted/clock-rollback timing, terminal partial settlement, and late full fills during the cancellation race.
- Updated deterministic partial-fill simulations to model grace expiry before remainder cancellation while retaining terminal fill reconciliation.
- Added [`docs/legacy/V3_8_0_BUY_PARTIAL_FILL_GRACE.md`](docs/legacy/V3_8_0_BUY_PARTIAL_FILL_GRACE.md), archived the v3.7.0 release note/report, and updated application, build, documentation, and test metadata to v3.8.0.
- v3.8.0 adds no SQLite table, column, index, migration, or persisted setting. Existing v3.7.0 databases and portable state remain compatible.

## v3.7.0

### Field-level market-data freshness

- Added independent bid, ask, Last, close, mark, and derived-price update/change identity to each production market-data snapshot. Same-price price ticks remain fresh; size-only and timestamp-only events do not refresh a cached price.
- Added selected-price basis metadata so `marketPrice` can be identified as Last-derived, quote-derived, mark-derived, or close-derived. A fresh whole-ticker callback no longer makes an unchanged Last fallback strategy-usable.
- Preserved pre-invalidation field values only as comparison baselines while clearing all update timestamps and sequences, so the first post-reconnect callback cannot make every cached field look fresh.
- Excluded unchanged fallback Last values from the controller price history and ATR input.

### Stage-3 final-SELL guard

- Required a complete, non-crossed, independently fresh bid/ask pair before the normal final SELL can be armed.
- Applied the configured maximum-spread percentage to Stage 3 even when the optional hard BUY-risk group is disabled.
- Required the executable SELL-side bid to confirm the recalculated rise trigger. Last, midpoint, mark, close, and `marketPrice` cannot independently arm the order.
- Required two consecutive distinct quote updates for the same cycle and market-data subscription. Any intervening invalid or non-quote event resets confirmation.
- Revalidated quote identity, bid, ask, field ages, spread, trigger, and order reference immediately before durable intent creation and again immediately before broker submission. A failed check rolls the unsubmitted transition back to Stage 3 and records `SELL_MARKET_DATA_REVALIDATION_BLOCKED`.
- Applied the same fresh complete spread-checked quote boundary to the optional profitable Stage-3 close-before-RTH price test.

### Regression coverage and compatibility

- Added incident-shaped regressions for CHIP's ask widening from 128.10 to 130.42 while Last remained 129.44, and for the later missing-bid event with ask near 128.04 and the same unchanged Last.
- Added coverage for same-price price ticks, size/timestamp-only events, field invalidation, independent quote-side ages, two-event confirmation, executable-bid reference selection, and both pre-submission revalidation boundaries.
- Added [`docs/legacy/V3_7_0_FIELD_LEVEL_MARKET_DATA_AND_STAGE3_SELL_GUARD.md`](docs/legacy/V3_7_0_FIELD_LEVEL_MARKET_DATA_AND_STAGE3_SELL_GUARD.md), archived the v3.6.0 release note/report, and updated application/build/documentation metadata to v3.7.0.
- v3.7.0 adds no SQLite table, column, index, migration, or persisted setting. Existing v3.6.0 databases and portable state remain compatible.

This file summarizes behavior-changing and maintenance releases represented by the repository. Historical implementation notes remain in `docs/legacy/` for traceability. Current behavior is documented in `README.md` and the current guides linked from `docs/README.md`.

## v3.6.0

### Fixed

- Required final SELL settlement to prove that persisted cycle executions exactly equal the app-owned BUY-filled quantity, that no broker remainder is still working, and that an aggregate fill price is available before completing the cycle.
- Persisted nonterminal final-SELL partials and kept them under order supervision. Terminal partials now enter `ERROR` with `SELL_PARTIAL_TERMINAL`; underfills, overfills, contradictory remainders, missing historical executions, and missing aggregate prices enter `ERROR` with `SELL_QUANTITY_MISMATCH`.
- Aggregated prior and replacement SELL executions for cancel-and-replace/manual-close exits so exact closure, weighted fill price, commission, and P/L remain correct when the final market order is sized only for the unsold remainder.
- Treated a partially filled SELL as working until its broker status is terminal, preventing a manual market-close request from overlapping a still-working remainder.
- Rejected a terminal protective SELL that reports both the full app-owned filled quantity and a positive remainder instead of waiting indefinitely or treating the position as exactly closed.
- Made formatted Trade History currency, percentage, and quantity columns sort by their underlying finite numeric values rather than display text.
- Reported audit-detail read and Trade History export failures in operator dialogs instead of allowing synchronous GUI-slot exceptions to escape or showing a false success message.

### Safety and compatibility

- Ambiguous final/protective SELL facts fail closed for manual reconciliation rather than completing a cycle without exact broker/local proof.
- Entry calculations, ATR behavior, order construction, sizing, market-data/RTH behavior, account/contract ownership, watchdog restart, and database schema are unchanged.
- v3.6.0 adds no SQLite table, column, index, migration, or persisted setting. Existing v3.5.0 databases and portable state remain compatible.

### Documentation and tests

- Added focused regressions for partial and contradictory SELL states, exact aggregate cancel-and-replace settlement, working-partial cancellation before manual replacement, numeric sorting, and GUI audit/export failures.
- Added [`docs/legacy/V3_6_0_SELL_RECONCILIATION_AND_HISTORY_ROBUSTNESS.md`](docs/legacy/V3_6_0_SELL_RECONCILIATION_AND_HISTORY_ROBUSTNESS.md), archived the v3.5.0 release note and implementation report under `docs/legacy/`, and updated current release/build metadata to v3.6.0.

## v3.5.0

### Changed

- Moved the **Price data monitor** above the connection and strategy configuration sections in Advanced and Debug views, matching the first operational section shown in Simple view.
- Changed startup appearance to always use the validated light Fusion palette. Dark mode remains available for the current session through **View > Dark mode**.

### Fixed

- Reapplied the current input-lock and workflow-button gates after light/dark theme switches. The bottom command buttons no longer remain disabled until the top lock is toggled twice.
- Made cached command-card updates reassert the underlying button enablement even when the semantic state text is unchanged.

### Safety and compatibility

- This release changes GUI layout and presentation state only. Trading strategy, order construction, controller behavior, recovery behavior, persistence, and broker ownership rules are unchanged.
- v3.5.0 adds no SQLite table, column, index, migration, or persisted setting. Existing v3.4.0 databases and portable state remain compatible.

### Documentation and tests

- Added focused regressions for Advanced-view widget order, forced light startup, retained manual theme switching, cached command-button enablement, and post-theme interaction-state reconciliation.
- Added [`docs/legacy/V3_5_0_GUI_LIGHT_MODE_AND_LAYOUT.md`](docs/legacy/V3_5_0_GUI_LIGHT_MODE_AND_LAYOUT.md), archived the v3.4.0 release note and implementation report under `docs/legacy/`, and updated current release/build metadata to v3.5.0.

## v3.4.0

### Fixed

- Changed Windows watchdog replacement to `subprocess.Popen` with an argument list so Python applies native Windows quoting when the executable or portable folder path contains spaces. POSIX replacement continues to use `os.execv`.
- Required protective SELL completion to prove an exact match between cumulative filled quantity and the app-owned BUY quantity with zero broker remainder. Live nonterminal partials remain under supervision; terminal partials enter `ERROR` with `PROTECTIVE_SELL_PARTIAL_TERMINAL`; inconsistent zero-remainder or overfill states enter `ERROR` with `PROTECTIVE_SELL_QUANTITY_MISMATCH`.
- Applied the same exact-quantity requirement to reconnect polling and both execution-led SELL recovery paths. Under-counted and over-counted recovered executions persist their observed facts but cannot complete the cycle.
- Prevented price-triggered exit logic from creating a second exit while a previously observed protective partial is temporarily unavailable from the broker poll.
- Loaded the Windows process API with `use_last_error=True` and explicit signatures so access-denied elevated lock owners are not misclassified as dead. Lock acquisition now closes the file descriptor and removes a partial lock file when the PID write fails.
- Kept the asynchronous market-capture writer alive after an individual write failure. Shutdown restarts a missing/dead writer when work remains and uses a monotonic hard deadline instead of unbounded `Queue.join()`.
- Removed only expired or malformed watchdog handoff files during tokenless startup while preserving fresh valid requests for the token-holding replacement.
- Required two consecutive matching cycle serializations before the GUI-thread fallback writes a resume checkpoint; continuously changing state now refuses the checkpoint instead of persisting an unconfirmed snapshot.
- Included the execution timestamp in the fallback execution-deduplication key, preserving distinct same-size, same-price partial prints when IBKR omits an execution ID.
- Made active-cycle selection deterministic for same-second updates with `updated_at DESC, cycle_number DESC, id DESC`.
- Removed a dead duplicate headless signal class, rounded the initial drop trigger to the strategy's four-decimal price precision, and normalized built-in TWS/IB Gateway Windows path literals.

### Safety and compatibility

- Strategy formulas, configured percentages, ATR behavior, price-source priority, normal order construction, position sizing, RTH rules, commission/P&L accounting, and account scope are unchanged.
- v3.4.0 adds no SQLite table, column, index, migration, or persisted setting. Existing v3.3.0 databases and portable state remain compatible through the existing startup and reconciliation paths.
- Ambiguous protective-exit quantities fail closed rather than being interpreted as cycle completion. The broker remains authoritative and manual reconciliation is required after a terminal partial or quantity mismatch.

### Documentation and tests

- Added focused deterministic regressions for all submitted fixes plus exact protective quantity mismatch handling, unavailable-poll suppression after a partial fill, continuously torn checkpoint refusal, malformed fresh watchdog requests, and shutdown with a missing writer reference.
- Added [`docs/legacy/V3_4_0_RELIABILITY_AND_RECOVERY_FIXES.md`](docs/legacy/V3_4_0_RELIABILITY_AND_RECOVERY_FIXES.md), archived the v3.3.0 release note and implementation report under `docs/legacy/`, and updated current release/build metadata to v3.4.0.

## v3.3.0

### Added

- Added automatic operating-system light/dark appearance. BouncyBot detects Qt's current system color scheme before applying its Fusion palette and follows supported live `QStyleHints.colorSchemeChanged` notifications without adding a persisted user setting.
- Added **View > Light mode** and **View > Dark mode** between the File and About menus for immediate in-process appearance switching. The checked menu item follows both manual changes and later system-theme notifications.
- Added dark-aware rendering for ordinary Qt controls and for the custom-painted Cycle Timeline, Profit Guard, strategy graph, and strategy flowchart surfaces, labels, grids, semantic states, and hover cards. The dark palette uses neutral Qt Fusion-style greys.
- Added a release-root `BouncyBot.lnk` shortcut to `GUI\IBKRTradingBot.exe`; it stores the GUI path as a relative fallback so the complete extracted release folder remains portable. `QUICK_START.txt` documents both launch paths and the shortcut is included in the release checksum manifest.
- Added an independent Qt-main-thread watchdog for the single controller worker. It warns after 3 seconds without a new controller snapshot, marks the worker unresponsive after 15 seconds, and requests a full-process replacement after 30 seconds. A dead worker triggers the same replacement path immediately after the startup grace period.
- Added `debug_reports/worker_emergency.log` for traceback and storage-fault diagnostics that do not depend on SQLite, plus one-time restart handoff and rate-limit history files outside the database.

### Changed

- Corrected the About dialog logo row by placing the bounded image in a dedicated fixed-height panel. The title/version/link rows are laid out after that panel and cannot overlap the artwork on high-DPI Windows displays.
- Kept bounded content widths for compact Cycle Audit Timeline fields while making both tables fill the available horizontal row. Their message columns stretch into remaining space, with a 3:2 transition-to-risk table allocation and conditional scrolling only when content genuinely exceeds the viewport.
- Removed the outer vertical scrollbar from the Cycle Audit Market capture tab. Its metadata table, captured-row preview, and capture-file list use bounded internal viewports and their own vertical scrollbars so all sections fit the main audit window.
- Stopped copying the complete source `Images/` directory into the Windows release root. PyInstaller still bundles the runtime application icon and About logo inside the `GUI/` one-directory bundle; source-only screenshots and README artwork remain in the repository/source release.
- Made controller event logging non-throwing. A SQLite failure now enters a visible fail-closed storage state and falls back to the plain emergency log instead of allowing error reporting itself to terminate the worker.
- Persisted market-driven waiting-stage transitions before publishing them in memory or executing the corresponding broker action. A failed persistence attempt leaves the previous cycle state authoritative and submits no order.
- During a storage fault, all strategy evaluation and broker-changing calls are blocked while the worker continues servicing the IBKR transport and emitting health snapshots. A short independent `BEGIN IMMEDIATE` transaction probes real write availability; normal automatic replacement begins only after that probe succeeds. A dead or hard-stalled worker still triggers process replacement even when the last snapshot reported a storage fault.
- Automatic recovery replaces the complete process with `os.execv`; it never starts a second controller thread or overlapping BouncyBot process. The replacement receives a short-lived one-time token and may resume only the exact previously monitored cycle when its persisted broker-relevant signature, cycle ID, stage, ticker, conId, and stored order references still match. The existing connection, exact-contract, broker-order, execution, position, and fresh-data reconciliation path must still succeed. Any mismatch remains fail-closed for manual reconciliation.
- Limited automatic replacement to three rapid attempts in a 15-minute window, followed by a five-minute cooldown between further attempts. Set `IBKR_BOT_AUTO_RESTART=0` to disable automatic replacement while retaining watchdog warnings.
- Added watchdog diagnostics to audit bundles. The one-time restart token is always redacted and the raw handoff file is never copied.
- Increased application, package, Windows release, documentation, and current regression metadata from v3.2.2 to v3.3.0.

### Safety and compatibility

- Strategy formulas, price-source priority, quantity calculations, order types, trailing/market order construction, RTH rules, risk limits, fill accounting, commission handling, P/L calculations, and account-position scope are unchanged.
- The single-worker ownership model is unchanged. The watchdog performs full-process replacement only after Qt exits and the portable-folder single-instance lock is released; it never runs two workers against the same cycle.
- Ordinary manual application startup remains conservative: a stored active cycle still requires explicit Start/reconciliation. Automatic resume is a narrow exception available only to the authenticated immediate replacement of a process whose final healthy snapshot proved that the exact cycle was already being monitored.
- No SQLite table, column, index, or migration was added. The write probe creates and inserts into a temporary candidate table only inside a transaction that is always rolled back. Existing v3.2.2 databases, settings, active cycles, orders, executions, audits, backups, exports, and market captures remain compatible.
- Process replacement cannot automate TWS/IB Gateway credentials, two-factor approval, Gateway startup, operating-system recovery, power restoration, or a frozen Qt main event loop. Uncertain startup or reconciliation facts remain manual rather than guessed.

### Documentation and tests

- Added focused coverage for light/dark palette roles, system-theme signal handling, View-menu theme switching, neutral Fusion-dark stylesheet conversion, theme refresh of cached state widgets and custom-painted views, non-overlapping About-logo layout, full-width Timeline table allocation, Market capture internal scrolling, runtime-only image packaging, shortcut creation/checksums, current release metadata, and archived v3.2.2 documentation.
- Added watchdog/storage regressions covering one-time handoffs, exact-cycle signatures, restart-loop protection, non-throwing diagnostics, real SQLite write probes, persist-before-publish ordering, broker-mutation blocking, transport-only callback pumping, dead/stalled worker detection, storage-fault recovery, GUI-side stale-age/RTH invalidation, process-lock release before replacement, exact-cycle reconciliation gates, and audit-token redaction.
- Updated [`docs/V3_3_0_DARK_MODE_AUDIT_AND_WINDOWS_RELEASE.md`](docs/legacy/V3_3_0_DARK_MODE_AUDIT_AND_WINDOWS_RELEASE.md), added [`docs/WORKER_WATCHDOG_AND_AUTO_RECOVERY.md`](docs/WORKER_WATCHDOG_AND_AUTO_RECOVERY.md), and retained the v3.2.2 release note under `docs/legacy/`.

## v3.2.2

### Changed

- Expanded the Live Strategy price-data monitor so the selected ticker appears above the current price in the same large type. A smaller line now shows the IBKR long name and available exact-contract information, including security type, SMART route, primary exchange, currency, `conId`, local symbol/trading class when useful, and available classification fields.
- Made Cycle Audit Timeline table columns content-sized instead of allowing a final column to stretch across unused width.
- Gave the stage-transition table the remaining Timeline width while limiting the guard/risk table to a bounded content width instead of an automatic 50/50 split.
- Top-aligned the Orders, Executions, and Decision events tables so compact records begin at the top of their tabs.
- Applied the supplied BouncyBot artwork as the source/runtime window icon and the Windows executable icon; the PyInstaller build now bundles the runtime logo/icon assets and copies `Images/` into the versioned release folder.
- Added an **About > Info** dialog with the BouncyBot logo, project GitHub link, current version, referral link, and the support addresses listed in the README.
- Added the BouncyBot logo immediately below the root README title and normalized the `Thank me` links and address labels.
- Moved the remaining superseded v3.0.17, v3.0.18, and v3.0.19 release-note files out of the root `docs/` directory and retained them under `docs/legacy/`.

### Safety and compatibility

- No strategy, order, risk, recovery, session, persistence, or P/L behavior changed.
- No SQLite schema object changed. Existing v3.2.1 databases remain forward-compatible.
- Contract name/classification values are read from the same `ContractDetails` response already required for qualification and are exposed only as read-only price-monitor metadata.
- Branding assets, the About dialog, build-resource inclusion, and documentation relocation do not change trading state, broker behavior, or the SQLite schema.

### Documentation and tests

- Added focused tests for contract-description propagation, price-monitor identity formatting and styling, content-sized Timeline columns, asymmetric Timeline table allocation, top-aligned audit record tabs, branding assets, application icon loading, About information, build resource inclusion, and legacy-document placement.
- Added [`docs/V3_2_2_GUI_INFORMATION_AND_AUDIT_LAYOUT.md`](docs/legacy/V3_2_2_GUI_INFORMATION_AND_AUDIT_LAYOUT.md) and archived the v3.2.1 release note under `docs/legacy/`.

## v3.2.1

### Fixed

- Added a conservative continuous-session overlay for `LSE` and `LSEETF`. Date-specific IBKR `liquidHours` still determine holidays, late opens, and earlier closes, but the effective session can no longer extend beyond 08:00-16:30 `Europe/London`. The corrected boundary drives RTH state, entry timing, active-BUY cancellation, Stage-3/Stage-4 pre-close liquidation, and RTH-only ATR collection.
- Prevented the 30-second RTH metadata cache from leaving an LSE/LSEETF contract marked open after its effective continuous close.
- Throttled repeated unchanged BUY preflight warnings by cycle and blocker category. The first warning is written immediately, repeated warnings are limited to one per 60 seconds, and the blocker continues to be enforced on every cadence.
- Added the distinct `PreflightBlocked` BUY status for conditions that prevent a live broker submission. `SubmitFailed` remains reserved for an actual submission attempt that fails before broker acceptance can be confirmed.

### Safety and compatibility

- The LSE/LSEETF policy only narrows IBKR's supplied session and fails closed if its verified continuous-session boundary cannot be applied. Other SMART primary exchanges continue to use their IBKR session metadata unless a separately verified venue policy is added.
- No SQLite schema object changes. Existing v3.2.0 databases remain forward-compatible; the new status value is stored in existing text fields.

### Documentation and tests

- Converted the three production-incident strict expected failures into ordinary passing regressions and added focused coverage for the VWRA continuous-close incident, earlier IBKR closes, non-LSE behavior, cache-boundary closure, malformed policy metadata, monotonic-time-zero audit logging, audit re-emission after the throttle interval, and `PreflightBlocked` persistence without an order intent.
- Added [`docs/V3_2_1_INCIDENT_GAP_CORRECTIONS.md`](docs/legacy/V3_2_1_INCIDENT_GAP_CORRECTIONS.md) and archived the v3.2.0 release note under `docs/legacy/`.

## v3.2.0

### Same-release bugscan correction

- Tightened exact-contract requalification so the broker-returned symbol, SMART order route, and selected primary exchange must still match the chosen API result. The qualified primary exchange is retained even when IBKR leaves the returned contract field blank.
- Corrected EUR market-data fallback variants so a qualified EUR contract cannot silently fall back to a USD request/cache key when the raw contract object omits its currency field.
- Made resume-checkpoint persistence enforce the database currency claim inside the same SQLite transaction as the settings and active-cycle checkpoint. A shutdown checkpoint can no longer bypass an existing USD/EUR database lock.
- Corrected the ten-second reconnect gate for deterministic clocks that begin at monotonic time zero; failed attempts now wait the full interval before retrying.
- Treated an exactly zero commission as currency-neutral and made non-zero cross-currency commission warnings persistently idempotent across restarts, including v3.2.0 events written before this correction.
- Corrected the two Ruff `I001` import-boundary spacing findings in `app/ib_adapter.py` and `app/storage.py`. No imports, runtime bindings, or release version were changed by those formatting corrections.

### Added

- Added support for exact ordinary `STK` contracts denominated in USD or EUR and routed through `SMART`. Live qualification requires a positive IBKR `conId` selected from the API search results and rechecks symbol, `conId`, currency, security type, SMART route, and primary exchange before monitoring or order submission.
- Scoped account-position lookups and contract-local risk/reinvestment calculations by exact positive `conId`; a same-symbol contract with a different `conId` is not treated as the selected position.
- Added one-contract-currency enforcement for each portable SQLite database. A new database may change its draft USD/EUR selection before the first cycle; the first persisted cycle locks the database currency. Existing USD databases infer that lock from their historical cycles.
- Added broker capability checks for SMART availability, required `MKT` and `TRAIL` order types, contract-specific market rules, whole-share minimum size/step rules, and date-specific `liquidHours`/`timeZoneId`.
- Added a fixed local API reconnect cadence: after an enabled TWS/IB Gateway socket connection is lost, BouncyBot retries every ten seconds indefinitely until it reconnects, the operator selects Disconnect, or the application shuts down.

### Safety and compatibility

- Non-U.S. contracts fail closed when IBKR session metadata is missing, invalid, or unparseable. The historical New York 09:30–16:00 fallback remains available only for recognized U.S. equity primary exchanges.
- BouncyBot does not perform FX conversion. Investment amounts, local risk totals, realized P/L, and reinvestment remain in the database's single contract currency. A non-zero commission reported in another currency is preserved as a raw broker fact, excluded from local net P/L, and disables Auto-repeat after the current cycle.
- BUY quantities may be rounded down to a valid whole-share size step before intent is persisted. SELL quantities are never rounded down because doing so could leave an untracked app-owned remainder. IBKR unset numeric sentinels are treated as unavailable size metadata rather than as enormous minimum quantities.
- Active-cycle recovery fails closed when the stored currency lock or exact contract identity cannot be verified. Manual Disconnect and shutdown continue to stop automatic reconnect attempts. Gateway/TWS upstream-server outages remain governed by the existing upstream reconciliation path rather than forcing repeated local-socket reconnects.
- No SQLite table or column is added. The database currency lock is stored in the existing `app_settings` key/value table, so v3.1.2, v3.1.1, v3.1.0, and v3.0.19 databases remain forward-compatible.

### Documentation and tests

- Added deterministic coverage for USD/EUR validation, exact contract search and qualification, SMART/order-type capability checks, European session parsing, non-U.S. RTH failure, size-step normalization, database currency inference and locking, cross-currency commission handling, GUI currency display, active-cycle recovery boundaries, and unlimited ten-second reconnect attempts.
- Added [`docs/V3_2_0_EUR_SMART_AND_RECONNECT.md`](docs/legacy/V3_2_0_EUR_SMART_AND_RECONNECT.md) and archived the v3.1.2 release note under `docs/legacy/`.

## v3.1.2

### Fixed

- Kept the original Stage-2 BUY order under supervision until IBKR reports a terminal state. A partial fill now remains in Stage 2, requests cancellation of only the unfilled remainder once, and continues reconciling cumulative fills received during the cancellation race before Stage 3 begins.
- Made execution and commission callback processing idempotent by exact IBKR execution ID. Execution details and commission reports may arrive in either order or be replayed after reconnect without duplicating quantity or commission. A stable residual cumulative-fill placeholder bridges order-status totals until individual execution IDs arrive, then shrinks or disappears as those callbacks are applied.
- Replaced shared-prefix ownership fallback with strict full-`OrderRef` ownership. Broker callbacks and Master-client open-order feeds are attached to a cycle only when the complete reference already exists in that portable installation's SQLite data. Unmatched `IBKRBOT|...` events remain unowned diagnostics and cannot change the active cycle or trigger cancellation.
- Corrected native trailing-order diagnostic throttling so changing market prices no longer create a new throttle identity every strategy tick. The stable key is cycle, side, and exact order reference.
- Corrected execution-time persistence. Live callbacks use ib_async's receipt timestamp as the canonical UTC execution timeline while preserving the broker-decoded execution time separately; recovered fills use the decoded broker time. This avoids the host-timezone double-offset seen in earlier audit exports.
- Extended **Cancel SELL trail and liquidate before close** to Stage 3. At the configured cutoff, BouncyBot submits an RTH-only `DAY` market SELL for the app-owned unsold quantity only when the selected current price is strictly above the average BUY fill price. Commissions are intentionally ignored for this eligibility comparison. If a protective SELL is working, it is cancelled and confirmed terminal before the remaining quantity is submitted.

### Safety and compatibility

- A Stage-3 profitability check is not an execution-price guarantee. The market fill may be below both the checked quote and the average BUY price. If the quote is no longer strictly profitable after a protective-order cancellation, the cycle stops in `ERROR` rather than transmitting a replacement SELL.
- Late BUY fills that arrive after an exit order has already been created, or execution-ledger SELL quantity above app-owned BUY quantity, stop the cycle in `ERROR` for manual review rather than silently understating or overselling the position.
- Existing Stage-4 cancel-confirm-replace behavior remains unchanged. No outside-RTH replacement is submitted.
- SQLite migration is additive and idempotent. v3.1.2 adds `cycles.buy_remainder_cancel_requested` with a default of false; existing v3.1.1, v3.1.0, and v3.0.19 databases remain forward-compatible.

### Documentation and tests

- Added focused deterministic coverage for partial-fill cancellation races, terminal cumulative fills followed by late callbacks, callback replay and ordering, late completed-cycle commissions, strict foreign-reference rejection, stable throttling, execution-time normalization, Stage-3 profitable-close boundaries, protective-order cancel/replace, restart recovery, and overfill safeguards.
- Added [`docs/V3_1_2_FILL_RECONCILIATION_AND_STAGE3_CLOSE.md`](docs/legacy/V3_1_2_FILL_RECONCILIATION_AND_STAGE3_CLOSE.md) and archived the v3.1.1 release note under `docs/legacy/`.

## v3.1.1

### Fixed

- Replaced contract-wide `minTick` order-price normalization with route-specific IBKR market-rule bands when `ContractDetails` advertises `marketRuleIds`. BUY stop and sizing prices round upward, SELL stop prices round downward, and the applicable increment is re-evaluated when rounding crosses a rule boundary.
- Changed IBKR what-if requests to use the dedicated `whatIfOrder` path with `whatIf=True` and `transmit=True`. Missing order state, validation/rejection status, rejection warnings, unset sentinels, and absent margin/equity output now fail closed; legitimate zero changes remain valid.
- Retained app-owned broker order-error callbacks, including code/message, order IDs, `OrderRef`, ticker, and advanced rejection JSON. The callback race cache is bounded and expiring, and manual orders are not attributed to BouncyBot.
- Added a no-fill rejection circuit breaker. `Inactive`, `Rejected`, or a terminal no-fill order with a substantive broker rejection now moves the cycle to `ERROR` for manual review instead of resetting Stage 2 and repeatedly submitting the same invalid order. Ordinary confirmed cancellations, including code 202 by itself, still reset an unfilled BUY to Stage 1.

### Safety and compatibility

- If IBKR advertises a market rule but the matching rule cannot be resolved or loaded, submission is blocked before broker transmission instead of falling back to the smallest contract-level tick.
- Broker order errors are persisted in the existing `broker_events` and `decision_events` audit streams; no SQLite schema migration is required. Existing v3.1.0 and v3.0.19 databases remain compatible.
- Strategy formulas, ATR settings, Stage-4 close-before-RTH liquidation, ownership calculations, fill accounting, and order types are otherwise unchanged.

### Documentation and tests

- Added focused deterministic coverage for IREN-style price bands, exchange/rule mapping, band-boundary rounding, rule caching and failure, strict what-if results, callback-order races, bounded error retention, manual-order isolation, broker-event persistence, cancellation distinction, and rejection stopping.
- Added [`docs/V3_1_1_IBKR_ORDER_VALIDATION.md`](docs/legacy/V3_1_1_IBKR_ORDER_VALIDATION.md) and archived the v3.1.0 release note under `docs/legacy/`.

## v3.1.0

### Added

- Added an optional **Cancel SELL trail and liquidate before close** policy in **Risk and timing**. It is disabled by default and uses a configurable **Liquidate before close** interval of 1–240 minutes, defaulting to 5 minutes.
- The policy applies only to the normal Stage-4 final SELL trailing-stop. When its cutoff is reached during the contract's confirmed RTH session, BouncyBot requests cancellation once, waits for a terminal broker status, and then submits one `DAY`, RTH-only market SELL for only the remaining app-owned quantity.
- Added persistent workflow state and audit decision events so cancellation races, partial fills, application restarts, and broker reconciliation can be handled without intentionally creating a second SELL while the original order may still work.

### Safety behavior

- A fill of the original trailing SELL during the cancellation race is accepted normally. If it closes the position, no replacement is submitted. If it partially fills, the replacement quantity is reduced to the unsold app-owned remainder.
- If cancellation is not confirmed before RTH closes, the original trailing SELL remains the only app exit order and no replacement is submitted.
- If cancellation is confirmed but an open RTH session can no longer be verified, or the replacement market order is rejected, cancelled, left incomplete at the close, or otherwise cannot be completed safely, the cycle enters `ERROR` for manual review. BouncyBot does not submit an outside-RTH fallback or silently recreate the trailing order.
- Market liquidation is unconditional once enabled and triggered; its execution price is not guaranteed and it may realize a loss. Normal Stage-5 completion and Auto-repeat behavior are preserved after a complete fill.
- Stage 1, Stage 2, Stage 3, protective SELL behavior, trailing-stop calculations, entry logic, and all existing risk controls are unchanged.

### Documentation and tests

- Updated the configuration, strategy, order-flow, risk, operations, recovery, limitation, database, troubleshooting, and verification guides for the new optional policy.
- Added focused regression coverage for defaults and validation, additive SQLite migration, GUI wiring, exact RTH/early-close timing, one-shot cancellation, cancellation/fill races, partial-fill aggregation, replacement-order attributes, failure handling, restart recovery, duplicate-SELL prevention, Stage-4 scope, and Auto-repeat behavior.
- Added [`docs/V3_1_0_CLOSE_BEFORE_RTH_LIQUIDATION.md`](docs/legacy/V3_1_0_CLOSE_BEFORE_RTH_LIQUIDATION.md), now archived for historical traceability.

## v3.0.19

### Fixed

- Removed the multi-second Trade History click-through delay caused by an unindexed ordered `events` lookup, repeated recursive capture-archive scans, eager capture ZIP parsing, and eager construction of every audit tab.
- Added ordered `cycle_id`/timestamp indexes for `events` and `decision_events`; existing databases receive the indexes through the additive schema initialization path.
- Changed current-format capture discovery to read only direct ZIP files from the selected ticker/cycle folder. Legacy/import fallback scanning now runs once only when the exact folder has no captures.
- Corrected cycle-folder matching so `cycle_1` no longer selects `cycle_10` through `cycle_19` as candidates.
- Made Timeline, Market capture, Orders, Executions, Decision events, and Raw log lazy tabs. Capture ZIPs are parsed only when Timeline or Market capture is first opened and the loaded result is shared by both tabs.
- Removed the application-defined 6× ceiling from both audit timeline graphs. Zoom now stops only at Qt's absolute widget-size boundary, with overflow-safe handling for extreme programmatic values.
- Replaced the minimal built-in history placeholder with a clearly labelled synthetic AAPL paper-trading cycle containing realistic entry, partial-execution, protective-order, final-exit, commission, decision-event, and market-capture data.
- Added a second **OK / Cancel** confirmation before the Stop strategy dialog can submit the app-owned unsold quantity as a market SELL. Cancel is the default, and the warning states that the fill may realize a loss and does not include unrelated account positions.
- Changed the current product display name to **BouncyBot - IBKR Portable Trading Bot** while retaining the established `IBKRTradingBot.exe` technical identifier for upgrade compatibility.
- Increased application, package, Windows release, documentation, and regression-test metadata from v3.0.18 to v3.0.19.

### Safety boundaries

- Strategy calculations, broker-side order construction/submission, fill handling, risk, reconciliation, persistence, recovery, and shutdown behavior are unchanged. The only stop-path change is the additional operator confirmation before the existing app-position market SELL request is accepted.
- The new indexes change SQLite query access paths only. Audit records and their ordering are unchanged.
- Trade-history inspection remains read-only and local; it does not call IBKR/TWS.

### Documentation and tests

- Added query-plan, exact-folder discovery, cycle-token isolation, deferred-loading, one-time capture-loading, unrestricted-zoom, realistic-example consistency, product-branding, and potential-loss market-SELL confirmation regressions.
- Added [`docs/legacy/V3_0_19_TRADE_HISTORY_AUDIT_PERFORMANCE.md`](docs/legacy/V3_0_19_TRADE_HISTORY_AUDIT_PERFORMANCE.md) and archived the v3.0.18 release note and implementation report under `docs/legacy/`.

## v3.0.18

### Changed

- Replaced the fixed one-second controller sleep with an interruptible command wait and independent monotonic deadlines for broker callbacks (50 ms), strategy evaluation (100 ms), GUI snapshots (500 ms), database snapshot reads (1 s), and maintenance (1 s).
- Changed scheduled quote reads to inspect existing subscriptions with a zero timeout. Explicit confirmation, start, and recovery paths retain bounded waits, but every price helper now checks the initial snapshot before sleeping and uses wait slices no longer than 50 ms instead of an unconditional 250 ms delay.
- Made periodic order polling nonblocking: cached trade state is returned immediately, and a cache miss may request a throttled open-order refresh whose callback is consumed by a later broker cadence.
- Moved read-heavy event, history, and GUI guard queries onto the database cadence and human-readable report generation onto the maintenance cadence. Safety-critical cycle, order, and execution persistence and live order-preflight queries remain synchronous.
- Increased the application, package, Windows release, documentation, and regression-test version metadata from v3.0.17 to v3.0.18.

### Safety boundaries

- Strategy formulas, order types, quantity calculations, fill handling, RTH checks at the final submission boundary, reconciliation rules, and backup behavior are unchanged. Broker and strategy work remain serialized on the single controller worker thread.
- The one-second database cadence applies only to read-heavy snapshot, history, and guard display data. Order intent, state transitions, fills, recovery facts, and resume checkpoints are still written immediately; BUY preflight reads the live SQLite ledger and risk totals rather than the GUI cache.
- Scheduled broker, quote, and order-state reads no longer sleep. User-requested operations that require a bounded broker response, including confirmation, start, recovery, cancellation, and what-if checks, may still wait explicitly.
- Shutdown now preempts an older queued command after the stop event is set, preventing a pending broker action from being executed during teardown.

### Documentation and tests

- Added event-driven scheduler, independent-cadence, immediate command wake-up, shutdown preemption, nonblocking market-data and order-polling, database-cache isolation, and live-preflight regression tests.
- Added a release-metadata consistency regression covering the GUI title, package version, Windows build version, current documentation, changelog, and release-note placement.
- Added [`docs/legacy/V3_0_18_EVENT_DRIVEN_CADENCES.md`](docs/legacy/V3_0_18_EVENT_DRIVEN_CADENCES.md) and archived the v3.0.17 release note under `docs/legacy/`.

## v3.0.17

### Changed

- Kept the Strategy flowchart **Flowchart data** selector visible in Simple, Advanced, and Debug modes.
- Preserved the selected completed cycle while live snapshots continue updating an active strategy, so previous trades remain inspectable during a running cycle.
- Simple mode continues to hide the explanatory paragraph; it no longer hides the flowchart data-source control.
- Changed the Windows `run_all_tests.bat` path to execute every collected pytest test in one Coverage.py run. The previous `not soak`/`soak` marker split and its intermediate deselection output were removed from the Windows gate.

### Safety boundaries

- The application change is GUI navigation only. Strategy, broker, order, fill, RTH, ATR, reconciliation, SQLite, backup, and history-record behavior are unchanged.
- The test-runner change affects developer validation only. The `soak` marker remains available for targeted runs, but the complete Windows gate no longer filters any pytest category.

### Documentation and tests

- Added focused visibility and active-cycle/history-selection regressions.
- Added test-infrastructure regressions requiring the Windows full-test path to contain one unfiltered pytest invocation and no separate soak-only pass.
- Completed a same-version public-repository documentation audit: current guides were corrected, superseded notes moved to `docs/legacy/`, a security policy and archive index were added, and generated/sensitive-file exclusions were expanded.
- Adopted the PolyForm Noncommercial License 1.0.0 and included `LICENSE` and `SECURITY.md` in assembled Windows release folders.
- Added [`docs/legacy/V3_0_17_FLOWCHART_HISTORY_SELECTOR.md`](docs/legacy/V3_0_17_FLOWCHART_HISTORY_SELECTOR.md).

## v3.0.16

### Changed

- Reorganized the Reconciliation tab into three explicit steps: **Refresh current broker facts**, **Compare SQLite with IBKR/TWS**, and **Resolve the situation**.
- Renamed **Resume** to **Reconcile and resume**, **Cancel app order** to **Cancel visible app-owned orders**, and **Refresh broker state** to **Refresh from IBKR/TWS**.
- Removed the remaining duplicate **Cancel app-owned open orders** button from the Advanced row. The guided cancellation path is now the only Reconciliation cancellation entry point and retains its confirmation and orphan-order handling.
- Added a visible broker-refresh status showing not-refreshed, current, stale, or failed state, the attempted refresh time, and the preceding successful refresh time after a failure.
- A successful refresh is current for at most 60 seconds and only while it remains connected, error-free, associated with the active cycle, and matched to the same reconciliation-relevant stage/order/fill facts. Price-only updates do not invalidate it; a disconnect, upstream outage, or later order/fill/stage/recovery change does.
- Disabled **Reconcile and resume**, cancellation, market SELL, and leave-orders-working until the broker refresh is current. The same check runs again in each click handler.
- Kept **Stop after current cycle** available as a local intent action and retained **Mark manually handled** as an explicit manual override with a stronger independent-TWS-verification warning. Audit export remains available.

### Safety boundaries

- No strategy calculations, RTH logic, quote selection, order types, order quantities, fill handling, SQLite trade persistence, backup behavior, or broker reconciliation algorithms changed.
- Refresh remains a read-only broker query. It does not synchronize SQLite automatically or submit, modify, or cancel an order.
- Manual handling still sends no broker instruction and must be used only after the operator has independently verified TWS when the app refresh is not current.

### Documentation and tests

- Added focused tests for refresh aging, failed-refresh retention, cycle/order/fill signature invalidation, price-only update tolerance, action gating, click-time checks, controller probe metadata, and duplicate-button removal.
- Added [`docs/legacy/V3_0_16_RECONCILIATION_REFRESH_WORKFLOW.md`](docs/legacy/V3_0_16_RECONCILIATION_REFRESH_WORKFLOW.md).

## v3.0.15

### Changed

- Exposed date-specific regular-session open/close boundaries parsed from IBKR contract `liquidHours` and the contract timezone.
- Changed first/last-minute BUY blockers and active-BUY cancellation-before-close to use those contract boundaries, including early-close days, instead of an independent hardcoded 16:00 close.
- Kept the existing conservative US-equity fallback inside the adapter for cases where IBKR does not return usable contract hours; new BUYs fail closed if no usable boundary exists, and automatic cancellation is not guessed.
- Made the plotted market-data capture interval the shared horizontal timestamp window for both Trade-history graphs, preventing older cycle or diagnostic timestamps from compressing the market path and visually misaligning action markers.
- Added matching vertical time guides across the market-data and app-actions plots; out-of-window timed actions are pinned to the nearest edge and disclosed in the graph text.

### Safety boundaries

- No strategy percentage, price-selection, order-type, quantity, fill, reconciliation, SQLite, backup, or recovery behavior changed outside the configured session-window timing described above.
- The base RTH check and broker `outsideRth=False` restriction remain in place.
- `CLOSED` contract days remain closed; split sessions retain the existing closed-gap behavior.

### Documentation and tests

- Added focused regressions for normal, early-close, closed, split-session, and fallback RTH boundaries; early-close BUY blocking and pre-close cancellation; and historical graph alignment with older action timestamps.
- Added [`docs/legacy/V3_0_15_RTH_AND_HISTORY_ALIGNMENT.md`](docs/legacy/V3_0_15_RTH_AND_HISTORY_ALIGNMENT.md).

## v3.0.14

### Changed

- Removed the duplicate **Stop after current cycle** and **Refresh broker state** controls from the Advanced row of the Reconciliation tab. The guided controls at the top remain the single UI entry points for those actions.
- Stored Trade-history **Cycle** cells as numeric Qt display values, so clicking the column header sorts numerically in both ascending and descending order instead of lexicographically.

### Safety boundaries

- No strategy, broker, order, fill, recovery-command, database, or persistence behavior changed.
- The remaining Advanced reconciliation controls retain their existing behavior and permission gating.

### Documentation and tests

- Added focused regression checks for reconciliation-button deduplication, numeric Cycle sorting, and v3.0.14 metadata.
- Added [`docs/legacy/V3_0_14_RECONCILIATION_HISTORY_SORTING.md`](docs/legacy/V3_0_14_RECONCILIATION_HISTORY_SORTING.md).

## v3.0.13

### Changed

- Made recurring GUI updates incremental: unchanged labels, status cards, stage/input-lock styling, graph inputs, and current-stage rows no longer trigger redundant Qt mutations or repolishing.
- Coalesced overlapping setting-preview refreshes with a 75 ms single-shot timer while retaining the separate 500 ms SQLite draft autosave timer.
- Deferred dashboard, flowchart, and Trade-history rebuilds while their tabs are hidden; the live chart continues collecting samples without requesting hidden repaints.
- Debounced Trade-history text filters by 200 ms, batched table construction with painting/sorting disabled, and limited content-based column sizing to once per newly received history data set.
- Reduced the periodic human-readable latest-state report from one write every 10 seconds to one write every 60 seconds to reduce OneDrive synchronization churn. Forced reports remain immediate.

### Safety boundaries

- No strategy, pricing, broker, order, fill, recovery, SQLite cycle/order/audit persistence, or backup behavior changed.
- The reduced write frequency applies only to `debug_reports/latest_state_report.txt`; application event logging and durable trading-state writes are unchanged.

### Documentation and tests

- Added focused regression coverage for visual-refresh coalescing, hidden-tab refresh behavior, deferred history rendering, hidden chart repaint suppression, flowchart change detection, version metadata, and the report interval.
- Added [`docs/legacy/V3_0_13_GUI_RESPONSIVENESS.md`](docs/legacy/V3_0_13_GUI_RESPONSIVENESS.md).

## v3.0.12

### Added

- Connected Qt's Windows session-management commit request to a non-interactive shutdown handler for update restarts, sign-out, and other orderly Windows shutdowns.
- Added an atomic resume checkpoint that stores the latest connection settings, strategy settings, active cycle, checkpoint metadata, and audit event before shutdown.
- Added a bounded direct-SQLite fallback when the worker cannot acknowledge the checkpoint in time. A shared checkpoint ID prevents duplicate audit records if both paths execute.

### Changed

- **Exit app and resume/recover later** and accepted main-window exit paths now force the same durable checkpoint before stopping the worker.
- Controlled Windows shutdown preserves the active cycle stage and app-owned broker orders. The session callback does not stop the worker or exit, so a cancelled Windows shutdown leaves the application usable. The next launch continues to require explicit operator Start/resume and reconciliation where applicable.
- Process-level cleanup now requests controller shutdown if the Qt event loop exits without a normal window-close path, while always releasing the single-instance lock even if cleanup fails.
- The Trade history **Summary** tab uses a compact six-column, four-row detail table without table scrollbars so the audit graph receives the remaining vertical space.
- The Trade history **Timeline** tab reserves only four visible rows in each lower table and lets the timeline graph expand vertically.

### Fixed

- Corrected the three Ruff `I001` import-block failures reported by `run_all_tests.bat`.
- Prevented shutdown checkpointing from re-evaluating a stored quote or causing a broker order side effect.
- Prevented the input-lock refresh from re-enabling **4. Start strategy** after command-bar logic had disabled it for an active cycle, ATR warmup, guard block, broker recovery, or operator input lock.
- Re-evaluated quote age on every GUI snapshot so a cached last price cannot remain green after actual streaming updates stop.
- Added explicit fail-closed handling for IBKR code 10197 (competing market-data session) and market-data farm messages 2103/2104. Cached fields remain visible for diagnosis but are invalid until a new `pendingTickersEvent` is consumed, and these quote-only messages cannot override a stronger full-upstream outage.

### Documentation and tests

- Documented the distinction between orderly Windows shutdown and an abrupt loss of power, including the existing non-persistence of ATR observations and incomplete market-data captures.
- Added storage, worker/fallback, Qt session-hook, GUI shutdown, idempotence, and version regression tests.
- Added 39 deterministic CSV price-path fixtures and expanded the catalog from 18 to 58 scenario contracts across 54 files without changing application source.
- Replaced the simulation runner's former known-stage check with exact expected lifecycle, event-order, quantity, fill-price, P/L, budget, RTH, protective-exit, and boundary assertions plus shared safety invariants.
- Added regressions for guard-blocked workflow-button state, frozen-green quote aging, competing-session/farm messages, fresh-event recovery, compact Summary details, and four-row Timeline tables.

## v3.0.11

### Fixed

- Separated the local application-to-TWS/Gateway API socket from the Gateway/TWS-to-IBKR server connection. IBKR system codes 1100 and 2110 now invalidate market data and pause strategy advancement, order polling, and new order submission even when the local socket remains connected.
- Handled code 1101 by discarding obsolete cached ticker handles and issuing new market-data subscriptions.
- Handled code 1102 by retaining the active subscription but requiring a new post-recovery ticker event before quote data can drive the strategy again.
- Replaced cached-field freshness with actual `pendingTickersEvent` identity and callback timestamps. Re-reading a populated `Ticker` no longer refreshes quote age, advances waiting stages, or feeds ATR/volatility history.
- Added post-restoration reconciliation of application-owned open orders and recent executions before normal worker processing resumes.
- Added fail-closed order-submission checks immediately before BUY, SELL, protective SELL, and market-close transmission.
- Blocked contract search, ticker confirmation, strategy start, broker refresh, cancellation, and market-close commands while the upstream link is unavailable; the workflow command bar now disables actions that require a fully ready broker session.
- Consumed a restoration message received during the synchronous connect/reconnect handshake through the same one-time reconciliation gate, avoiding a redundant second reconciliation on the following worker tick.

### Changed

- The Connection and Data indicators distinguish local socket state, upstream IBKR state, post-reconnect reconciliation, fresh-event wait, cached-only fields, and stale actual updates.
- ATR observations use the original broker callback time rather than the later controller read time, preventing delayed event processing from moving an old quote into a newer bar bucket.
- Native broker orders already accepted before an outage are not cancelled merely because connectivity is lost; their status and fills are recovered after connectivity returns.

### Documentation and tests

- Documented upstream-only outages, 1100/1101/1102 recovery, cached quote behavior, operator recovery, and remaining availability limits.
- Added focused tests for event identity, stale-event age, cached-read exclusion, subscription recreation/retention, worker-loop pausing, command and order blocking, one-time handshake recovery, workflow-button gating, and post-reconnect reconciliation.
- Expanded the deterministic test suite without changing application source: every effective executable callable under `app/` and in `main.py` is now entered by at least one test, statement/branch coverage is gated at 75%, and machine-readable coverage reports plus a per-callable gate are part of `run_all_tests.bat`.
- Added a test-only offline behavior layer for broker-event permutations, generated controller invariants, property-style numeric/payload checks, recovery matrices, differential simulation, multi-instance isolation, crash/restart and migration cases, storage fault injection, Gateway outage sequences, bounded soak tests, and a six-mutant safety smoke gate. The application source remains unchanged.

## v3.0.10

### Fixed

- Stopped the GUI from rewriting **Maximum spread %** from live bid/ask-derived suggestions. The saved field now changes only through explicit user edits or loading persisted user settings.
- Reconciled point-in-time broker probes with newer terminal order polls so a completed cycle is not falsely shown as having a working app order.
- Made the Stop dialog, main-window exit path, and Reconciliation tab use the persisted application-owned fill ledger for unsold quantity. External account holdings do not create a market-SELL option.
- Disabled recovery action buttons during ordinary configured guard pauses and normal strategy waits. Read-only broker refresh and audit export remain available.

### Changed

- ATR RTH observations and diagnostic bars continue to accumulate while ATR adaptation is disabled. Disabling adaptation prevents percentage changes; it does not discard current-session RTH observations.
- Replaced per-snapshot ATR history rescans with bounded incremental RTH OHLC aggregation, while retaining short snapshot reuse for duplicate high-frequency reads. Every usable RTH observation is still collected.
- Removed the duplicate dashboard **Controls** panel. The **Recovery / audit log** now uses the full dashboard width in Simple, Advanced, and Debug modes; the fixed five-button command bar remains the workflow control surface.

### Documentation and tests

- Updated the README and current operating guides for the fixed spread setting, ATR collection semantics, safe completed-cycle exit, guard-versus-recovery behavior, and dashboard layout.
- Added focused regression coverage for spread immutability, ATR collection with adaptation disabled, completed-cycle probe retirement, app-owned exit quantities, recovery-button gating, and full-width audit layout.

## v3.0.9

### Maintenance

- Corrected the final Ruff import-block spacing finding in the v3.0.8 regression test.
- Added a regression check for the expected import-to-constant spacing.
- No trading, broker, storage, or GUI behavior changed.

### Documentation

- Replaced the release-note-oriented README with a GitHub-ready project guide covering behavior, advanced features, boundaries, dependencies, installation, operation, testing, and packaging.
- Audited source comments and docstrings against v3.0.9 behavior without changing executable Python AST or script/config commands.
- Rewrote the current architecture, strategy, order, risk, recovery, database, testing, and operations guides.
- Added a documentation index, configuration reference, limitations, troubleshooting, changelog, and repository `.gitignore`.
- Marked older release-specific files as historical so they cannot be mistaken for the current operating specification.

## v3.0.8

### Changed

- The input lock disables the five workflow buttons as well as editable settings.
- The Trading status reports all evaluated BUY/SELL blockers through a compact label and detailed tooltip.
- ATR entry warmup leaves Stage 1 without an initial-drop trigger when the warmup block is enabled; readiness establishes a fresh anchor.
- New BUY checks use unsold quantity from application-recorded fills rather than the account-wide IBKR position. External/manual long positions no longer block a new application cycle.

## v3.0.7

### Fixed

- PowerShell no longer mistakes captured PyInstaller log lines for the process exit code. A successful build reaches the normal success message; a real nonzero exit still fails.

## v3.0.6

### Changed

- The Account field became optional. Blank leaves `Order.account` unset; an explicit account remains a validated routing override.

## v3.0.5

### Fixed

- Corrected the remaining import order in `app/gui.py` without behavior changes.

## v3.0.4

### Fixed

- Corrected the Windows batch result path so Ruff/Pyright failures cannot also print a false overall success.
- Applied reported import-order cleanup, removed two unused local assignments, and corrected the typed JSON fallback.

## v3.0.3

### Changed

- Ruff and Pyright became installed, required quality gates in the standard Windows test launcher.

## v3.0.2

### Changed

- Added Ruff and Pyright to the project dependency collection and invoked them through the active Python interpreter.
- Made missing quality tools a failure in the standard Windows validation path.

## v3.0.1

### Fixed

- Long IBKR connection-status messages wrap instead of widening the connection settings area.

## v3.0

### Added

- Reconciliation-oriented recovery screen and audit-bundle export.
- Restore-validated, rotated SQLite backups.
- Stale-active-cycle detection and explicit recovery gating.
- Conservative Ruff and Pyright configuration.
- Larger-database and property-style strategy regression tests.

## Earlier releases

Earlier `V2_*` and `LEGACY_*` documents record the incremental introduction of the five-stage GUI, recovery, guard, ATR, capture, timeline, Windows runtime, and build behavior. Those files are historical records rather than operating instructions. Consult the current documentation before relying on a historical description.
