# Automated test coverage specification

This document defines the automated verification scope for v3.9.0. It is the maintainer-facing map between the application modules, test layers, and repository quality gates.

The v3.9.0 offline test architecture includes focused coverage for audit diagnostic coalescing, live Stage-3 guard status, reconnect/native-order wait aggregation, BUY partial-fill grace/timeout safety, shutdown checkpoints, event-driven worker scheduling, independent cadences, nonblocking broker reads, GUI responsiveness, broker connectivity, reconciliation, flowchart history selection, the optional Stage-3/Stage-4 close-before-RTH workflows, market-rule price normalization, strict what-if interpretation, broker error retention, rejection circuit breaking, exact USD/EUR SMART contract selection, one-currency database enforcement, atomic resume-checkpoint currency validation, qualified-currency market-data fallbacks, persistent commission-mismatch idempotence, contract capability/session validation, and fixed ten-second indefinite reconnect behavior. Tests use temporary databases, deterministic clocks and data, protocol-shaped broker doubles, and headless Qt doubles. They do not connect to IBKR, launch TWS/Gateway, or transmit orders.

## Test objectives

The automated suite applies six complementary checks:

1. **Behavioral assertions** verify expected outputs, state transitions, persistence, generated order payloads, error handling, and fail-closed behavior.
2. **Statement and branch coverage** measures exercised application paths and prevents the combined coverage percentage from dropping below 75%.
3. **Per-callable entry coverage** requires every executable application function, method, property getter, and nested helper reported by Coverage.py to execute at least one statement.
4. **Bounded deterministic soak tests** exercise high-volume buffers, histories, cycles, and reconnect sequences. The complete Windows gate includes them in the same Coverage.py run as every other pytest test.
5. **Safety mutation smoke tests** require focused probes to detect seventeen deliberate financial/state-machine defects in temporary copies.
6. **Deterministic CSV simulations** run complete price-path scenarios through the strategy simulator independently of the unit-test fakes.

Per-callable entry coverage is intentionally not described as complete path coverage. A function can contain mutually exclusive branches, external failure modes, timing races, or platform-specific behavior that require additional tests or manual verification. The line/branch report and assertions remain necessary.

## Current module inventory

The callable gate is derived from the effective function map in `coverage.json`. Shadowed definitions that are not part of the imported runtime module are not counted. `app/__init__.py` contains no executable callables.

| Application module | Executable callables entered | Primary automated focus |
|---|---:|---|
| `app/controller.py` | 246 / 246 | Event-driven command queue, independent broker/strategy/database/GUI/maintenance cadences, lifecycle, connectivity, guards, recovery, execution reconstruction, order-side effects, snapshots |
| `app/flowchart_model.py` | 10 / 10 | Stage-card construction, labels, details, filtering |
| `app/gui.py` | 373 / 373 | Formatting, blocker/recovery classification, runtime theme styling, custom-painted views, widget state, command gating, timelines, panels, dialogs, layout helpers |
| `app/ib_adapter.py` | 134 / 134 | Data normalization, event ownership, connectivity, market data, contracts, orders, executions, positions |
| `app/ib_platform.py` | 11 / 11 | Profiles, path discovery, socket probing, process-launch outcomes |
| `app/lockfile.py` | 8 / 8 | Acquisition, stale-lock handling, release, context-manager behavior |
| `app/market_data_capture.py` | 22 / 22 | Bounded buffers, capture lifecycle, serialization, asynchronous write behavior |
| `app/models.py` | 45 / 45 | Validation, serialization, pricing/profit formulas, ATR adaptation, dataclass compatibility |
| `app/order_diagnostics.py` | 3 / 3 | Native trailing-order diagnostics and trigger interpretation |
| `app/paths.py` | 8 / 8 | Source/packaged runtime paths, branding resources, and generated directories |
| `app/simulation.py` | 5 / 5 | Simulation state, fill assumptions, result serialization |
| `app/storage.py` | 80 / 80 | Schema migration, CRUD, ledger queries, exports, backup/restore validation |
| `app/strategy.py` | 23 / 23 | Five-stage transitions, fills, partial fills, editable settings, error states |
| `app/timeline_scaling.py` | 28 / 28 | Parsing, filtering, robust bounds, downsampling, marker/time-axis placement |
| `app/watchdog.py` | 17 / 17 | Emergency diagnostics, authenticated restart handoff, stale-request cleanup, restart rate limiting |
| `main.py` | 11 / 11 | Light-mode startup, palette/theme helpers, application icon, single-instance startup, watchdog replacement, window lifecycle, cleanup |
| **Total** | **1,024 / 1,024** | All effective executable application callables |

The counts are a snapshot of v3.9.0. The gate recalculates them from the current source and coverage report on every full test run. Adding a callable without a test causes the callable-coverage step to fail.

The v3.9.0 source tree executed **1,211/1,211** collected pytest cases across all 126 test modules with `ResourceWarning` promoted to an error. Every test module also passed in a separate fresh pytest process. The run measured **78.1%** combined statement/branch coverage and entered **1,024/1,024** executable application callables. The release also killed **17/17** targeted safety mutants and passed **58/58** deterministic simulation contracts across 54 CSV price paths.

## Test layers

### v3.1.0 close-before-RTH layer

Focused tests cover default-off configuration, validation and SQLite migration, exact contract-session timing, one-shot cancellation, full/partial fill races, remaining-quantity calculation, `DAY`/RTH-only market replacement attributes, failure-to-`ERROR` behavior, restart/recovery continuity, manual-close conflict prevention, Auto-repeat preservation, and static GUI/documentation wiring.

### v3.1.1 broker-validation layer

Focused tests cover exchange-to-market-rule mapping (including empty positional rule IDs), price-band selection, side-aware exact-decimal rounding, boundary re-evaluation, cache reuse, fail-closed unresolved rules, strict `whatIfOrder` status/margin interpretation, legitimate zero values, broker error callback ownership and race handling, manual-order isolation, bounded expiration, unrelated request-error exclusion, audit persistence, and the no-fill rejection circuit breaker versus ordinary cancellation.


### Pure unit tests

Pure functions and dataclasses are tested with explicit normal, boundary, invalid, and compatibility inputs. These tests cover strategy mathematics, validation, timeline scaling, diagnostics, profile normalization, serialization, and state-copy behavior.

### Stateful component tests

SQLite, controller, capture, and locking tests use `pytest` temporary directories. Each test receives an isolated database/path and does not depend on state left by another test. Persistence assertions read the stored records back rather than only inspecting in-memory objects.

### Broker-boundary tests

`tests/test_comprehensive_ib_adapter.py` and controller tests use protocol-shaped fakes for IB, contracts, tickers, orders, trades, executions, positions, and connectivity events. Tests assert the translated request/response data at the application boundary. No fake is presented as proof of real IBKR server behavior.

### Headless GUI tests

`tests/support/qt_stubs.py` supplies deterministic Qt-compatible doubles. GUI tests assert state classification, labels, enablement, model-to-widget updates, dialog decisions, timeline construction, and paint/event entry points. Screenshot appearance, native font metrics, accessibility tooling, and operating-system window behavior remain manual-test concerns.

### Simulation tests

`tests/simulated_data/` contains 54 fixed price-path files. `tests/simulation_scenario_catalog.py` defines 58 independently named contracts over those files. `scripts/run_all_simulations.py` executes the complete catalog in one process and fails on any schema, catalog-registration, stage, event-order, quantity, fill-price, payload, P/L, budget, RTH, or shared-invariant mismatch. The corresponding parameterized pytest module makes the same contracts visible as individual test cases.

### Generated, fault, replay, and soak tests

The expanded deterministic layers cover callback permutation, generated controller sequences, numerical/payload properties, recovery decision matrices, simulation equivalence, multi-instance isolation, abrupt subprocess termination, schema migration, backup and filesystem failures, upstream connectivity sequences, and bounded high-volume operation. [`OFFLINE_BEHAVIOR_TESTS.md`](OFFLINE_BEHAVIOR_TESTS.md) maps these tests to their invariants and exclusions.


### Production-incident replay and migration layer

Sanitized fixtures derived from the available IREN, NBIS, and VWRA audit evidence drive the real controller, strategy, adapter-normalization, and storage boundaries. They cover market-rule price rejection, BUY multi-print/grace and cancellation races, late commissions, strict cross-instance ownership, delayed market-data blocking, Stage-3 executable-stop rounding, and the LSE continuous-close mismatch. Synthetic databases created by v3.0.19, v3.1.1, and v3.1.2 verify additive, idempotent migration into the current storage layer. Fixture provenance, privacy checks, resolved-incident regressions, and the audit-bundle sanitizer are documented in [`PRODUCTION_INCIDENT_REPLAY_TESTS.md`](PRODUCTION_INCIDENT_REPLAY_TESTS.md).

### Test-infrastructure self-tests

`tests/test_test_infrastructure.py` verifies the callable gate itself, requires the Windows launcher to run one unfiltered pytest invocation, and confirms that the Unix launcher retains its explicit coverage/soak stages. This prevents a future script edit from silently bypassing a test category.

### v3.1.2 fill reconciliation and Stage-3 close layer

Focused tests cover terminal BUY settlement after partial fills, cancellation-race cumulative fills, residual cumulative placeholders, duplicate/reordered execution and commission callbacks, completed-cycle commission enrichment, exact foreign-reference rejection, stable native-trail throttling, live/recovery execution timestamps, Stage-3 strict-profit cutoff checks, protective-order cancel-confirm-replace, restart continuity, and quantity-conflict fail-closed behavior.

### v3.2.0 exact-contract, currency, and reconnect layer

Focused tests cover exact positive `conId` selection, USD/EUR ordinary `STK` validation, SMART routing, contract identity mismatch rejection, required `MKT`/`TRAIL` capability metadata, price/quantity increment handling, non-US `liquidHours` and timezone fail-closed behavior, one contract currency per SQLite database, legacy USD inference, mixed-currency rejection, exact-conId risk and position scoping, commission-currency mismatch handling without FX conversion, currency-aware GUI/flowchart presentation, and fixed ten-second reconnect attempts with no retry limit until manual disconnect or shutdown.

### v3.2.0 EUR SMART and reconnect layer

Focused tests cover exact positive `conId` selection, USD/EUR ordinary-`STK` validation, SMART-only routing, exact identity requalification, required `MKT`/`TRAIL` capabilities, price and quantity increments, non-U.S. session fail-closed behavior, European time zones, one-currency database migration/locking, currency-aware GUI presentation, commission-currency mismatch handling, initial-connection retry, repeated ten-second reconnect attempts without a limit, successful recovery reset, and manual Disconnect/shutdown cancellation.

### v3.2.0 same-release bugscan layer

Eleven regressions cover broker-returned symbol/route/primary-exchange mismatches, retention of a selected primary exchange when IBKR omits it, EUR fallback-market-data request identity, atomic shutdown-checkpoint currency enforcement, the reconnect interval at monotonic time zero, zero-valued cross-currency commissions, persistent commission-mismatch deduplication after a controller restart, and compatibility with mismatch events written by the earlier v3.2.0 build. Static release tests also pin the exact Ruff-safe import boundaries reported by Ruff 0.16.0.

### v3.2.1 production-incident correction layer

Focused tests convert the three former strict expected failures into ordinary regressions. They verify the 08:00-16:30 `Europe/London` continuous-session cap for `LSE` and `LSEETF`, preservation of earlier IBKR closes, unchanged behavior for other primary exchanges, fail-closed malformed policy metadata, effective-close enforcement through the short RTH cache, stable BUY-preflight condition coalescing including monotonic time zero, no repeated price-normalization audit rows while delayed data is already known to block the BUY, and the distinct `PreflightBlocked` status without an order intent.

### v3.9.0 audit diagnostic coalescing layer

Focused tests cover stable Stage-3 reason codes and condition keys, suppression of changing age/message text, one-minute and five-minute persistence summaries, one recovery event, structured occurrence/reason/max-metric payloads, and a 705-observation NBIS-style stale-ask sequence. They also cover GUI-only non-price callbacks, immediate near-trigger invalid evidence, confirmation invalidation, normal 15-minute native-order summaries, native-stop anomalies, reconnect aggregation/recovery, repeated BUY-preflight blockers, inactive-condition pruning, and Price Data Monitor presentation. These tests prove that audit persistence is bounded while the underlying guard and reconnect cadences continue unchanged.

### v3.8.0 BUY partial-fill grace and safety layer

Focused tests cover native trailing and zero-trail market BUYs, no cancellation on the first positive partial, normal multi-print completion inside the fixed 3.0-second grace, timeout cancellation measured from the first persisted fill, persistence across reload, and proof that later partial progress does not restart the grace clock. Safety cases verify immediate cancellation when enabled RTH, live-data, stale-data, session-boundary/pre-close, recent-volatility, minimum-price, previous-close gap, or bid/ask spread controls become unsafe or cannot be verified. Additional regressions cover cancellation retry and duplicate suppression, future-timestamp recovery, broker-terminal partial settlement, and full late-fill reconciliation during the cancellation race. The deterministic simulation layer advances partial scenarios to the conceptual grace timeout before modelling a successful remainder cancellation.

### v3.7.0 field-level market-data and Stage-3 SELL guard layer

Focused tests reproduce the CHIP cycle-3 incident states: a bid of 127.82 with an ask widening to 130.42 while Last remains 129.44, and a missing bid with ask near 128.04 while the same Last remains cached. They verify per-field update versus numerical-change identity, same-value price ticks, exclusion of size/timestamp-only callbacks, post-reconnect timestamp invalidation, selected-price basis classification, stale-Last exclusion from ATR, independent bid/ask ages, Maximum spread enforcement with hard BUY-risk limits disabled, executable-bid trigger confirmation, two distinct qualifying quote observations, confirmation reset on intervening invalid events, and both pre-intent and pre-broker revalidation gates.

### v3.6.0 SELL reconciliation and Trade History robustness layer

Focused tests cover nonterminal and terminal final-SELL partials, contradictory terminal remainder data, exact aggregate SELL closure across cancel-and-replace orders, missing persisted execution proof, weighted aggregate fill price and commission, cancellation of a still-working partially filled order before manual replacement, protective full-fill/remainder mismatch handling, finite numeric sorting for formatted history cells, and operator-visible audit-read/export failures. These tests use isolated SQLite databases, deterministic broker doubles, and headless Qt widgets; they do not transmit orders.

### v3.5.0 Fusion themes, branding, and audit-layout layer

Focused tests cover forced light-mode startup, retained manual **View > Light mode / Dark mode** switching, light and neutral Fusion-dark palette roles, stylesheet conversion, menu check-state synchronization, post-theme workflow-button and input-lock reconciliation, refresh of cached semantic widgets, price-monitor-first Advanced/Debug layout, dark-aware custom-painted charts, qualified-contract long-name and classification propagation, exact instrument-identity formatting in the price monitor, the dedicated non-overlapping About-logo panel, bounded Timeline columns with full-width 3:2 table allocation, Market capture internal scrollbars without an outer page scrollbar, and top-aligned Orders, Executions, and Decision events tabs. The GUI helpers and lazy tab builders are entered through headless Qt doubles; native Windows visual rendering remains a manual validation item.

## Full validation sequence

`run_all_tests.bat` performs the Windows gate in this order:

1. Create or reuse `.venv`.
2. Install `requirements.txt`, including Coverage.py.
3. Compile `app`, `tests`, `scripts`, and `main.py`.
4. Erase stale coverage data.
5. Run every collected pytest test, including tests marked `soak`, with `ResourceWarning` promoted to an error while collecting statement and branch coverage. No pytest marker filter is applied.
6. Enforce the 75% combined statement/branch threshold.
7. Write `coverage.json` and `coverage.xml`.
8. Run `scripts/check_callable_coverage.py`; every effective executable application callable must have been entered.
9. Run `scripts/run_mutation_smoke.py`; every configured safety mutant must be killed.
10. Run every deterministic CSV simulation.
11. Run Ruff with the configured correctness/import rules.
12. Run Pyright with the configured type-checking scope.
13. Return a nonzero exit code if any stage fails.

The Unix-like `scripts/run_tests.sh` performs compilation, non-soak coverage, callable entry, bounded soak, mutation smoke, and CSV simulation stages. Ruff/Pyright remain part of the complete Windows `run_all_tests.bat` gate.

## Generated test artifacts

The following files are replaced on each run and are not source artifacts:

| File | Contents |
|---|---|
| `run_tests_pytest.log` | Pytest output collected under Coverage.py |
| `run_tests_coverage.log` | Statement/branch coverage table and threshold result |
| `run_tests_callable_coverage.log` | Per-callable gate result and any missing callable names |
| `run_tests_mutation_smoke.log` | Safety mutation probes and kill result |
| `run_tests_simulations.log` | Deterministic CSV simulation output |
| `.coverage` | Coverage.py binary data file |
| `coverage.json` | Machine-readable line, branch, class, and function coverage |
| `coverage.xml` | CI-compatible Cobertura XML report |

These generated files should not be committed unless a CI/release process explicitly requires an artifact. The repository `.gitignore` excludes the runtime forms.

## Failure interpretation

- A **pytest failure** means an asserted behavior, invariant, or regression contract did not hold.
- A **coverage-threshold failure** means too many application statements/branches are unexercised, even if all assertions passed.
- A **callable-coverage failure** identifies a specific application callable that no test entered.
- A **soak-test failure** appears in the main pytest log and means a configured history/buffer/resource bound or high-volume invariant did not hold.
- A **mutation failure** means a deliberate safety defect survived its independent contract probe or the mutation target drifted unexpectedly.
- A **simulation failure** means a deterministic scenario no longer produced its expected lifecycle.
- A **Ruff failure** identifies configured syntax, import, or likely-defect issues.
- A **Pyright failure** identifies a type-checking error in the configured core scope.

Do not resolve a coverage failure with blanket exclusions, `noqa`, or an assertion-free call merely to increment a counter. Add a test that states the callable's contract, includes at least one meaningful assertion, and exercises relevant boundary/failure behavior.

## Adding or changing application behavior

For each changed callable:

1. Identify its observable contract and side effects.
2. Add a normal-case test.
3. Add boundary and invalid-input tests where the callable accepts external/user/broker data.
4. Add failure-path tests for I/O, broker, database, parsing, and connectivity boundaries.
5. Use a temporary database/path and deterministic fakes; do not share mutable test state.
6. Add a CSV simulation when the change affects a complete strategy price path.
7. Run `run_all_tests.bat` and inspect both coverage reports, not only the final pass line.
8. Perform the relevant manual paper-account checks in [`TEST_PLAN.md`](TEST_PLAN.md) when the change touches real TWS/Gateway behavior or native GUI behavior.

## Limits of automated verification

The automated suite cannot prove:

- real exchange fills, slippage, queue priority, or trigger behavior;
- IBKR permissions, margin, market-data entitlements, or server-side order handling;
- every timing interleaving during a real network or process failure;
- Windows native rendering and user interaction on every display/DPI configuration;
- profitability or suitability for live trading.

Those limits are addressed through paper-account integration testing, audit inspection, and the manual test plan rather than by weakening the distinction between a deterministic fake and the external system.
