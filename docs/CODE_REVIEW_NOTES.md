# Maintainer review notes

This file records the current review boundaries for v5.3.0. It is not a release changelog and should not be used instead of the behavioral guides.

## Source-of-truth order

For a disputed behavior, review in this order:

1. executable source and current automated tests;
2. `README.md` and current guides in `docs/README.md`;
3. the current release entry in `CHANGELOG.md`;
4. archived notes under `docs/legacy/` only for implementation context.

Historical notes can accurately describe the release that introduced a feature while still being incomplete for current behavior.

## High-risk review areas

Changes in these areas require focused strategy, recovery, and failure-path review:

- order ownership and `IBKRBOT|` filtering;
- BUY/SELL quantity calculations and partial-fill handling;
- protective/final/market SELL cancellation sequencing;
- optional account routing and managed-account validation;
- app-owned versus account-wide position logic;
- exact conId/currency/SMART contract qualification, one-database currency ownership, contract session/size metadata, RTH, stale-data, session, ATR, strict what-if, route-specific market-rule pricing, broker rejection, and hard-risk blockers, including the user-owned Maximum spread setting;
- recovery after missing callbacks or reconnect, including fixed 10-second retry behavior, exact-contract requalification, database-currency checks, and ordering between point-in-time probes and newer terminal polls;
- SQLite migration, execution deduplication, backup validation, and writable-directory assumptions;
- minimum-tick rounding and stop reference prices;
- zero-trail market-order branches.

## Architectural expectations

- `StrategyEngine` remains pure: no Qt, SQLite, or live broker calls.
- The controller remains the single broker-side-effect coordinator.
- The GUI remains a command/display layer and does not duplicate strategy decisions. Its read-only audit reader receives detached data and returns plain results; only the main Qt thread creates or updates widgets. Closing or destroying the dialog cancels pending work.
- Cross-cycle audit context requires validated fill metadata, a bounded time window and matching instrument identity. It must never change cycle/order ownership or stored records.
- RTH-restricted actions require authoritative contract hours/timezone metadata; neither the adapter nor the GUI may invent a schedule when that evidence is missing.
- Storage migrations remain additive and idempotent; one portable database remains single-currency and never mixes USD/EUR totals.
- Broker cancellation and execution facts are not inferred from local intent alone.
- Expected guard pauses remain visually distinct from reconciliation errors and do not expose recovery-changing actions without an independent mismatch.
- A normal guard or strategy wait does not expose Reconcile and resume, cancellation, market-SELL, leave-orders-working, or manual-handling recovery permissions; refresh and export remain read-only.
- A newer terminal order poll may remove the same order from an older cached broker probe, but a later explicit probe must never be hidden.
- Stop, window close, and Reconciliation use the same persisted app-owned fill ledger and never infer app ownership from the account-wide position.
- ATR observation and bar collection remain independent of the adaptation toggle; applying adaptive settings is not.
- Live quote data may determine whether the spread guard is blocked, but must never write the Maximum spread configuration field.
- The Strategy flowchart data selector remains available in Simple, Advanced, and Debug modes and preserves an explicitly selected completed cycle while live snapshots continue.

## Connectivity and quote-freshness invariants

- `IB.isConnected()` represents only the local application-to-TWS/Gateway socket.
- IBKR connectivity events drive the separate upstream availability state.
- `pendingTickersEvent` identity and callback time define whole-event freshness; raw price tick types plus value comparison define independent Last/bid/ask/mark/close update identity.
- Waiting strategy stages and ATR/volatility history consume each subscription sequence once, and the generic selected price is actionable only when its underlying raw basis updated in that event.
- The normal Stage-3 final SELL requires independently fresh bid and ask, a valid configured spread, executable-bid trigger confirmation on two distinct quote updates, and revalidation before both intent and broker submission.
- A restoration that loses market-data requests replaces subscription handles; a restoration that retains requests still invalidates prior update metadata until a new event arrives.
- Broker-order and execution reconciliation completes before normal post-restoration strategy processing.
- Every order-transmission path rechecks connectivity immediately before placement.
- An accepted native order is not cancelled merely because connectivity is interrupted. A lost local API endpoint is retried every 10 seconds indefinitely until manual Disconnect or shutdown.

Preserve focused tests for late callbacks, duplicate cached reads, same-price fresh events, restoration races, and point-in-time reconciliation ordering.

## Compatibility names

Do not rename persisted fields without a migration and compatibility plan:

- `rise_trigger_pct` means user-facing **Minimum profit %**;
- `max_cycles_per_ticker_day` currently means a total completed-cycle cap for the ticker;
- old SQLite files may omit later additive fields and rely on dataclass defaults.

Comments and documentation should explain current semantics rather than repeat obsolete UI labels.

## Testing expectations

A behavior change should include the smallest relevant combination of:

- pure model or strategy test;
- controller failure-path test;
- storage migration or recovery test;
- GUI blocker or layout regression;
- deterministic CSV simulation;
- Windows script regression.

Run the complete Windows gate (`run_all_tests.bat`) under standard GIL-enabled CPython 3.14.x before distribution. A successful pytest run is insufficient when Ruff or Pyright fails.

## Documentation maintenance

When behavior changes:

1. update relevant inline comments and docstrings without adding release-history prose to active modules;
2. update the current README and relevant guide;
3. update `CONFIGURATION_REFERENCE.md` for defaults or semantics;
4. update `LIMITATIONS.md` when the support boundary changes;
5. add a concise changelog entry;
6. add a release note only when traceability warrants it, and archive superseded notes under `docs/legacy/`;
7. keep examples and formulas aligned with the source;
8. avoid promises of fill price, stop price, or profit.

## Publication and distribution boundary

The public-repository documentation set:

- keeps the application, package and current documentation version aligned at v5.3.0;
- keeps current operational material in `docs/` and superseded release notes in `docs/legacy/`;
- treats SQLite files, backups, audit bundles, reports, captures, screenshots, and broker/account data as private unless deliberately sanitized;
- uses the unmodified PolyForm Noncommercial License 1.0.0 text in the repository root;
- includes `LICENSE` and `SECURITY.md` in assembled Windows release folders;
- does not describe the project as OSI-approved open source because commercial use is restricted;
- requires license terms to accompany redistributed copies as specified by the license.

Documentation-only maintenance must not alter application runtime source, strategy rules, broker actions, storage schemas, or GUI behavior.

## v4.0.0 review boundary

`atr_memory.py` validates and checkpoints only volatility estimates. It cannot generate a price event or broker action. The controller keeps the established live ATR formula, excludes preceding-window bars from a fresh RTH calculation, and retains same-contract raw history for the separate recent-volatility guard. `order_edit_policy.py` defines a strict reviewed field allowlist and exact-cycle edit intent. Stage-2 and Stage-4 working-order policy is unchanged. Existing market-data selection, partial fills, order construction, ownership, reconciliation, and watchdog replacement code are retained.

The optional ATR record falls back to ordinary warmup on validation/read failure. Write failures keep the live calculation available and produce non-throwing emergency diagnostics with bounded retry frequency. GUI changes do not remove the real minimum-profit guard. Native Windows appearance and a two-session paper-account test remain required outside this Linux/headless validation environment.


## v4.1.0 GUI-only review

Only `app/gui.py` changes at runtime. All other `app` modules and `main.py` are checked byte-for-byte against the corrected v4.0.0 source. No new settings, database schema, order action, broker request or accounting ledger value is written. Invested is a read-only BUY-fill/commission calculation; Position allocates that cost to existing app-owned unsold inventory and never adopts external account holdings. Existing history row identity and numeric sorting are preserved. The history table no longer grows its minimum height with row count. Decision events releases compact-table maximum dimensions and stretches its Message column. That release introduced linked Timeline cursors; this behavior is superseded by v5.0.0 independent cursors. The tests explicitly distinguish Qt contract doubles from native rendering validation.


## Retained v4.2.0 GUI-only review

Review is relative to the supplied v4.1.0 Status Order Linked Crosshairs source. Runtime edits in `app/gui.py` cover status header layout/font, removal of Timeline cursor synchronization, responsive Reconciliation/Summary table sizing, shared compact-table fitting, and current version text. Existing data loading, price scaling, timeline mapping, zoom/scroll, Timeline tables and local hover behavior remain. No non-GUI application module, broker action, persisted setting or database schema change is required. The prior note/report are archived byte-for-byte; current measured verification and platform limits belong in the root implementation/test report.

`ContentFitTable` queues a guarded refit after row/column geometry, model data, resize, font or style changes. Reconciliation uses uncapped full-row fitting inside a scrollable page; its eight comparison rows have no table scrollbar. The guided actions use a two-by-two grid and the advanced action box has no fixed maximum height. Audit Summary keeps its existing height cap and enables vertical scrolling for overflow. Shared fit helpers reserve horizontal-scrollbar height where relevant and do not hide capped overflow. These source-level layout policies do not establish native rendering correctness; real Qt and Windows DPI checks remain unperformed on this host.

## v5.0.0 trading-safety review

The runtime patch is limited to adapter/controller/strategy/model/storage boundaries and the requested GUI behavior. It does not consolidate the architecture or alter unrelated ATR/capture/watchdog modules. Each fault correction has a focused regression and adjacent valid-path coverage, with independent review of submission uncertainty, identity, recovery, protection and market subscriptions. The original callback-GC allegation and several audit-only suggestions were excluded from this patch. Current measured results and unavailable native/runtime gates belong in the root implementation report; the preceding GUI-only scope is historical.

## v5.1.0 implementation review

All eleven confirmed defect groups from the 25 September review receive narrow changes and positive/negative regression coverage. Runtime changes are confined to controller, broker snapshot metadata, storage, one strategy protection condition and GUI session/confirmation/sizing behavior. Version/test/build metadata and affected operating guides are updated separately.

Six implementation scopes were independently cross-reviewed. Review follow-ups covered legacy execution raw-data types, authoritative zero/foreign-currency fee precedence, restart during partial SELL cancellation, completed-date migration and old test snapshots whose measured ages disagreed with their wall timestamps. The excluded shutdown observation and Linux-qualified folder-lock issue were not implemented. See the root verification report for final measured results and unavailable tools.


## v5.3.0 review boundary

This release is relative to 5.2.0. Runtime presentation edits belong in `app/gui.py`: the empty ticker placeholder, graph/monitor order, lock-driven command-bar visibility, measured flowchart geometry and the right-side Reconciliation selector. Reconciliation retains its existing page and index; the native corner button replaces only its left-side tab header. Lock hiding must retain the existing disabled states and restore workflow gating after unlock or theme changes. Flowchart layout must fit all five cards at their existing font sizes, including cards excluded by the current view filter.

No strategy, broker, storage, timer, dependency or persisted-setting change belongs in this release. Confirm that those modules match 5.2.0 and retain their tests. Native widget checks and manual Windows scaling checks complement Qt-double regressions; list executed checks and remaining limits in the root report.
