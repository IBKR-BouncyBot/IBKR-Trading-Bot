# Automated test coverage specification

This document defines the automated verification scope for v3.0.19. It is the maintainer-facing map between the application modules, test layers, and repository quality gates.

The v3.0.19 offline test architecture includes focused coverage for shutdown checkpoints, event-driven worker scheduling, independent cadences, nonblocking broker reads, GUI responsiveness, broker connectivity, reconciliation, and flowchart history selection. Tests use temporary databases, deterministic clocks and data, protocol-shaped broker doubles, and headless Qt doubles. They do not connect to IBKR, launch TWS/Gateway, or transmit orders.

## Test objectives

The automated suite applies six complementary checks:

1. **Behavioral assertions** verify expected outputs, state transitions, persistence, generated order payloads, error handling, and fail-closed behavior.
2. **Statement and branch coverage** measures exercised application paths and prevents the combined coverage percentage from dropping below 75%.
3. **Per-callable entry coverage** requires every executable application function, method, property getter, and nested helper reported by Coverage.py to execute at least one statement.
4. **Bounded deterministic soak tests** exercise high-volume buffers, histories, cycles, and reconnect sequences. The complete Windows gate includes them in the same Coverage.py run as every other pytest test.
5. **Safety mutation smoke tests** require focused probes to detect six deliberate financial/state-machine defects in temporary copies.
6. **Deterministic CSV simulations** run complete price-path scenarios through the strategy simulator independently of the unit-test fakes.

Per-callable entry coverage is intentionally not described as complete path coverage. A function can contain mutually exclusive branches, external failure modes, timing races, or platform-specific behavior that require additional tests or manual verification. The line/branch report and assertions remain necessary.

## Current module inventory

The callable gate is derived from the effective function map in `coverage.json`. Shadowed definitions that are not part of the imported runtime module are not counted. `app/__init__.py` contains no executable callables.

| Application module | Executable callables entered | Primary automated focus |
|---|---:|---|
| `app/controller.py` | 176 / 176 | Event-driven command queue, independent broker/strategy/database/GUI/maintenance cadences, lifecycle, connectivity, guards, recovery, execution reconstruction, order-side effects, snapshots |
| `app/flowchart_model.py` | 9 / 9 | Stage-card construction, labels, details, filtering |
| `app/gui.py` | 337 / 337 | Formatting, blocker/recovery classification, widget state, command gating, timelines, panels, dialogs, layout helpers |
| `app/ib_adapter.py` | 99 / 99 | Data normalization, event ownership, connectivity, market data, contracts, orders, executions, positions |
| `app/ib_platform.py` | 11 / 11 | Profiles, path discovery, socket probing, process-launch outcomes |
| `app/lockfile.py` | 8 / 8 | Acquisition, stale-lock handling, release, context-manager behavior |
| `app/market_data_capture.py` | 22 / 22 | Bounded buffers, capture lifecycle, serialization, asynchronous write behavior |
| `app/models.py` | 44 / 44 | Validation, serialization, pricing/profit formulas, ATR adaptation, dataclass compatibility |
| `app/order_diagnostics.py` | 3 / 3 | Native trailing-order diagnostics and trigger interpretation |
| `app/paths.py` | 7 / 7 | Source/packaged runtime paths and generated directories |
| `app/simulation.py` | 5 / 5 | Simulation state, fill assumptions, result serialization |
| `app/storage.py` | 62 / 62 | Schema migration, CRUD, ledger queries, exports, backup/restore validation |
| `app/strategy.py` | 21 / 21 | Five-stage transitions, fills, partial fills, editable settings, error states |
| `app/timeline_scaling.py` | 28 / 28 | Parsing, filtering, robust bounds, downsampling, marker/time-axis placement |
| `main.py` | 3 / 3 | Stable palette setup, single-instance startup, window lifecycle, cleanup |
| **Total** | **835 / 835** | All effective executable application callables |

The counts are a snapshot of v3.0.19. The gate recalculates them from the current source and coverage report on every full test run. Adding a callable without a test causes the callable-coverage step to fail.

## Test layers

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

### Test-infrastructure self-tests

`tests/test_test_infrastructure.py` verifies the callable gate itself, requires the Windows launcher to run one unfiltered pytest invocation, and confirms that the Unix launcher retains its explicit coverage/soak stages. This prevents a future script edit from silently bypassing a test category.

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
