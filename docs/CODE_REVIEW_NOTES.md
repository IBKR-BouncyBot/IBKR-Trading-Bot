# Maintainer review notes

This file records the current review boundaries for v3.0.17. It is not a release changelog and should not be used instead of the behavioral guides.

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
- RTH, stale-data, session, ATR, what-if, and hard-risk blockers, including the user-owned Maximum spread setting;
- recovery after missing callbacks or reconnect, including ordering between point-in-time probes and newer terminal polls;
- SQLite migration, execution deduplication, backup validation, and writable-directory assumptions;
- minimum-tick rounding and stop reference prices;
- zero-trail market-order branches.

## Architectural expectations

- `StrategyEngine` remains pure: no Qt, SQLite, or live broker calls.
- The controller remains the single broker-side-effect coordinator.
- The GUI remains a command/display layer and does not duplicate strategy decisions.
- Storage migrations remain additive and idempotent.
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
- `pendingTickersEvent` identity and callback time, not repeated reads of populated ticker fields, define quote freshness.
- Waiting strategy stages and ATR/volatility history consume each subscription sequence once.
- A restoration that loses market-data requests replaces subscription handles; a restoration that retains requests still invalidates prior update metadata until a new event arrives.
- Broker-order and execution reconciliation completes before normal post-restoration strategy processing.
- Every order-transmission path rechecks connectivity immediately before placement.
- An accepted native order is not cancelled merely because connectivity is interrupted.

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

Run the complete Windows gate (`run_all_tests.bat`) before distribution. A successful pytest run is insufficient when Ruff or Pyright fails.

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

- keeps the application and package version at v3.0.17 for documentation-only revisions within this release;
- keeps current operational material in `docs/` and superseded release notes in `docs/legacy/`;
- treats SQLite files, backups, audit bundles, reports, captures, screenshots, and broker/account data as private unless deliberately sanitized;
- uses the unmodified PolyForm Noncommercial License 1.0.0 text in the repository root;
- includes `LICENSE` and `SECURITY.md` in assembled Windows release folders;
- does not describe the project as OSI-approved open source because commercial use is restricted;
- requires license terms to accompany redistributed copies as specified by the license.

Documentation-only maintenance must not alter application runtime source, strategy rules, broker actions, storage schemas, or GUI behavior.
