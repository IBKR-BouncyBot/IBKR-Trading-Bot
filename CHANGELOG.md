# Changelog

This file summarizes behavior-changing and maintenance releases represented by the repository. Historical implementation notes remain in `docs/legacy/` for traceability. Current behavior is documented in `README.md` and the current guides linked from `docs/README.md`.

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
- Added [`docs/V3_0_19_TRADE_HISTORY_AUDIT_PERFORMANCE.md`](docs/V3_0_19_TRADE_HISTORY_AUDIT_PERFORMANCE.md) and archived the v3.0.18 release note and implementation report under `docs/legacy/`.

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
- Added [`docs/V3_0_18_EVENT_DRIVEN_CADENCES.md`](docs/V3_0_18_EVENT_DRIVEN_CADENCES.md) and archived the v3.0.17 release note under `docs/legacy/`.

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
- Added [`docs/V3_0_17_FLOWCHART_HISTORY_SELECTOR.md`](docs/legacy/V3_0_17_FLOWCHART_HISTORY_SELECTOR.md).

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
