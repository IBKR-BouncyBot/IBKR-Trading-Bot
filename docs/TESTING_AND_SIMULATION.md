# Testing, simulation, and quality gates

The v5.3.0 GUI layer covers the N/A ticker placeholder, the embedded graph position, footer removal and monitor ordering in all modes, command-bar hiding and restoration under the GUI lock, equal text-fitting flowchart cards and Reconciliation navigation. See the [GUI release note](V5_3_0_GUI_LAYOUT.md#verification-boundaries) and root implementation report for actual execution results and native-platform limits.

The 5.2.0 storage changes have focused regressions in `test_v520_manual_marker_index.py` and `test_v520_backup_validation.py`. They cover unchanged safety-query results and application rows, ordinary startup backups, committed WAL contents, legacy restore migrations, failure handling, retention and audit export. See the [archived release note](legacy/V5_2_0_STORAGE_PERFORMANCE.md#verification-and-measurement-boundaries) for the coverage map and [5.2.0 report](legacy/V5_2_0_IMPLEMENTATION_TEST_REPORT.txt) for that release's measured results. The root implementation report records the current checks and unavailable gates.

The repository includes pure-model tests, controller/storage integration tests, protocol-shaped broker-boundary tests, sanitized production-incident replays, historical migration fixtures, generated event/state tests, crash and fault-injection tests, bounded soak tests, mutation smoke tests, headless GUI component tests, deterministic CSV simulations, and Windows build-script checks. The automated suite does not require a live IBKR session. The test-scope map, historical coverage snapshot and gate semantics are documented in [`AUTOMATED_TEST_COVERAGE.md`](AUTOMATED_TEST_COVERAGE.md).

## Windows full validation

Run from the project root:

```powershell
.\run_all_tests.bat
```

The source, test and build scripts require standard GIL-enabled CPython 3.14.x. They reject Python 3.14t, other Python branches and incompatible existing virtual environments before installing dependencies. Rename an incompatible `.venv` while BouncyBot is closed, then rerun the launcher; keep the database and other application files in place. `requirements.txt` defines the shared runtime, test, quality and packaging dependencies.

The launcher:

1. creates/reuses `.venv` through `scripts/run_tests.ps1` and verifies its Python runtime;
2. upgrades `pip` and installs `requirements.txt`;
3. sets Qt/headless test environment variables for the process;
4. compiles `app`, `tests`, `scripts`, and `main.py`;
5. erases stale Coverage.py data;
6. runs every collected pytest test, including bounded soak tests, with `ResourceWarning` treated as an error while collecting statement and branch coverage; no pytest marker filter is applied;
7. enforces a 75% combined statement/branch minimum;
8. writes `coverage.json` and `coverage.xml`;
9. requires every effective executable application callable to be entered through `scripts/check_callable_coverage.py`;
10. runs the deterministic safety mutation smoke gate;
11. runs every deterministic CSV simulation through one Python process;
12. runs Ruff against `app` and `tests`;
13. runs Pyright using `pyproject.toml`;
14. returns nonzero if any required stage fails.

Detailed output is written to:

```text
run_tests_pytest.log
run_tests_coverage.log
run_tests_callable_coverage.log
run_tests_mutation_smoke.log
run_tests_simulations.log
coverage.json
coverage.xml
```

The batch file prints `QUALITY CHECKS PASSED` only when both required quality tools return zero.

## Individual commands

After dependencies are installed in a compatible environment:

```powershell
.\.venv\Scripts\python.exe scripts\check_python_runtime.py
.\.venv\Scripts\python.exe -m compileall -q app tests scripts main.py
.\.venv\Scripts\python.exe -m coverage erase
.\.venv\Scripts\python.exe -X utf8 -W error::ResourceWarning -m coverage run --branch --source=app,main -m pytest -q --tb=short -ra --disable-warnings
.\.venv\Scripts\python.exe -m coverage report --show-missing --fail-under=75
.\.venv\Scripts\python.exe -m coverage json -o coverage.json
.\.venv\Scripts\python.exe -m coverage xml -o coverage.xml
.\.venv\Scripts\python.exe scripts\check_callable_coverage.py --coverage-json coverage.json --source app --source main.py
.\.venv\Scripts\python.exe scripts\run_mutation_smoke.py
.\.venv\Scripts\python.exe scripts\run_all_simulations.py
.\.venv\Scripts\python.exe scripts\run_quality_checks.py --require-tools
```

For a single test:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_strategy.py -q
```

On Unix-like development systems:

```bash
./scripts/run_tests.sh
```

That shell script first verifies the Python runtime, then runs compilation, non-soak pytest coverage, the statement/branch threshold, the per-callable gate, bounded soak tests, the mutation smoke gate, and CSV simulations. It does not perform Ruff, Pyright, or Windows PyInstaller packaging and does not replace `run_all_tests.bat` as the complete Windows gate.

## Test environment isolation

The Windows test launcher sets:

- UTF-8 I/O;
- `QT_QPA_PLATFORM=offscreen`;
- a Windows font directory when available;
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`;
- `IBKR_BOT_HEADLESS_SIGNALS=1`;
- no-bytecode behavior for a cleaner tree.

It restores the prior process environment afterward. The development launcher explicitly clears test-only values and forces the real Windows Qt platform so a GUI run is not accidentally headless.

## Test categories

### Pure strategy/model tests

Validate:

- Stage-1 anchor reset and drop trigger;
- zero and positive BUY trail behavior;
- whole-share quantity and slippage planning;
- minimum-profit formulas;
- zero and positive SELL trail behavior;
- partial fills;
- protective SELL transitions;
- ATR calculation/adaptation/readiness, including continued RTH collection while adaptation is off;
- validation and serialization.

### v3.1.2 broker-boundary regressions

The focused release suite verifies market-rule exchange mapping, positional rule IDs, price-band boundaries, exact BUY-up/SELL-down normalization, failure when advertised rules cannot be loaded, strict what-if behavior, callback-race association, manual-order isolation, audit persistence, and rejection-versus-cancellation state transitions. The tests reproduce the structural IREN invalid-price failure without connecting to IBKR.

### v3.2.0 USD/EUR SMART and reconnect regressions

The focused v3.2.0 suite verifies exact API-selected positive `conId` identity, USD/EUR ordinary-stock and SMART-only validation, required contract capability metadata, European exchange-session parsing and fail-closed missing schedules, one-currency database locking and legacy USD inference, exact-conId ledger/risk scoping, no-FX commission mismatch handling, quantity increments, currency-aware presentation, and fixed ten-second reconnect attempts that continue until connection, manual Disconnect, or shutdown.

### v3.2.1 incident-gap correction regressions

The focused v3.2.1 suite verifies the 08:00-16:30 `Europe/London` continuous-session cap for `LSE` and `LSEETF`, preservation of an earlier IBKR close, unchanged non-LSE behavior, fail-closed malformed policy metadata, effective-close enforcement through the short RTH cache, immediate and stable repeated-preflight audit throttling, suppression of redundant price-normalization audit rows while delayed data already blocks a BUY, and the `PreflightBlocked` status without an order intent.

### v3.9.0 audit diagnostic coalescing regressions

The focused v3.9.0 suite verifies stable diagnostic condition keys and reason codes, changing-age suppression, condition entry/summary/recovery lifecycle, structured counts and maximum metrics, and bounded one-minute/five-minute/15-minute cadence. An NBIS-shaped 705-observation stale-ask sequence is reduced to a small bounded audit sequence while retaining every observation in the live condition state. Additional tests cover GUI-only cached/non-price callbacks, immediate near-trigger invalid evidence, invalidated first SELL confirmation, native trailing-order normal/anomaly cadence, reconnect aggregation and recovery, BUY-preflight recovery, and Price Data Monitor rendering. Existing Stage-3 field freshness, two-observation confirmation, pre-submit revalidation, order handling, risk, recovery, and persistence tests remain unchanged.

### v3.8.0 BUY partial-fill grace and safety regressions

The focused v3.8.0 suite verifies the revised Stage-2 settlement policy for both native trailing and zero-trail market BUYs: the first positive partial remains working, a complete multi-print fill inside the fixed 3.0-second grace is not cancelled, the first-fill timestamp survives reload, later partial progress does not restart the grace clock, and a still-nonterminal remainder receives one cancellation after timeout. Safety-path tests cover RTH closure, required live-data loss, stale data, unavailable or pre-close session timing, recent-volatility limits, minimum-price and previous-close-gap limits, and unavailable, crossed, or excessive bid/ask spreads. The suite also covers failed-cancel retry, duplicate suppression, future-timestamp recovery, broker-terminal partial settlement, and full late-fill reconciliation after a cancellation request. The historical NBIS multi-execution replay confirms that a second fill can complete normally during the grace rather than being raced by an immediate cancellation.

### v3.7.0 field-level market-data and Stage-3 SELL regressions

The focused v3.7.0 suite reproduces both critical CHIP cycle-3 market states and verifies the complete nine-step correction: independent bid/ask/Last timestamps, raw selected-basis freshness, complete quote and spread enforcement, executable-bid trigger confirmation, two distinct observations, confirmation reset, revalidation before intent and broker submission, ATR exclusion of unchanged fallback Last values, and exact incident-shaped regression data. Additional tests cover same-value price ticks, size/timestamp-only callbacks, reconnect invalidation, delayed-field compatibility, and the Stage-3 close-before-RTH quote boundary.

### v3.6.0 SELL reconciliation and Trade History regressions

The focused v3.6.0 suite verifies persistence and continued supervision of nonterminal final-SELL partials; fail-closed terminal partial, overfill, contradictory remainder, missing-history, and missing-price states; exact aggregate completion across earlier and replacement SELL orders; cancellation before manual close replacement; protective terminal full-fill/remainder mismatch handling; raw-value sorting for formatted Trade History numeric cells; and operator dialogs for audit-detail and export failures. It enters the real controller/storage boundaries with deterministic broker doubles and the GUI helpers through the headless Qt layer.

### v3.5.0 Fusion themes, branding, and Cycle Audit layout regressions

The focused v3.5.0 suite verifies forced light-mode startup, retained manual **View > Light mode / Dark mode** switching and check-state synchronization, light and neutral Fusion-dark palette roles, deterministic stylesheet conversion, refresh of cached semantic widgets, post-theme workflow-button/input-lock reconciliation, dashboard ordering (configuration, integrated price monitor, then market/strategy state in Advanced and Debug; monitor first in Simple), dark-aware custom-painted views, the dedicated non-overlapping About-logo panel, propagation of IBKR long-name and classification fields, exact instrument-identity formatting, bounded content-sized Timeline columns with full-width 3:2 table allocation, Market capture internal scrollbars without an outer page scrollbar, top-aligned audit tabs, runtime-only Windows image packaging, release-root shortcut creation, and documentation placement. It runs the relevant builders and theme helpers through the headless Qt layer rather than relying only on static source assertions.

### Controller tests

Use test adapters/headless signals to validate:

- command queue and lifecycle, including immediate wake-up and shutdown preemption;
- independent broker, strategy, GUI, database-snapshot, and maintenance cadences;
- zero-timeout scheduled price/order reads, bounded short-slice explicit waits, and fail-closed broker-before-strategy ordering;
- cached GUI database facts versus live SQLite order preflight;
- RTH/data/session/hard-risk blockers;
- Trading blocker reporting;
- account-routing behavior;
- app-owned position scope;
- order payload normalization;
- what-if/submission failure rollback;
- reconnect and recovery, including retirement of stale probe rows by newer terminal polls;
- 1100/1101/1102 upstream connectivity, subscription recreation/retention, actual event identity/age, cached-read exclusion, fail-closed missing-event handling, worker pausing, stale SELL presentation, and order-submission gating;
- guard-versus-recovery action gating;
- stop/window-close/market-close sequencing based on the persisted app-owned ledger;
- Stage-2 BUY partial-fill grace, timeout/safety cancellation, and late-fill reconciliation;
- capture/report hooks.

### Storage tests

Validate:

- additive schema migration from older databases;
- cycle/order/execution/event persistence;
- execution deduplication;
- app-owned unsold quantity queries;
- history metrics and exports;
- online backup and restore validation;
- audit-bundle contents.

### GUI regression tests

GUI coverage combines source/layout regressions with deterministic headless Qt doubles. It preserves important labels, button gating, tooltips, version metadata, timeline behavior, Reconciliation layout, state classification, model-to-widget updates, dialog decisions, paint/event entry points, local-versus-upstream connection presentation, actual-update/cached-only diagnostics, and post-recovery data-pending states without requiring an interactive display.

A passing source regression does not replace manual visual testing on Windows.

### Coverage gates

Coverage.py records both statements and branches for `app/` and `main.py`. The aggregate gate fails below 75%. A separate generated function map requires entry into every effective executable application function, method, property getter, and nested helper. This callable gate is recalculated on each run, so a newly added application callable without a corresponding test fails the full suite.

The callable gate proves entry, not exhaustive path coverage. Assertions, branch coverage, failure-path tests, simulations, and manual IBKR/Windows integration remain separate requirements.

### Deterministic offline behavior expansion

The non-GUI offline expansion adds broker callback permutations, generated controller invariants, numerical/payload properties, recovery decision matrices, differential simulation, multi-instance isolation, subprocess crash/restart tests, schema migration and restore fixtures, sanitized production-incident replays, storage fault injection, Gateway outage sequences, bounded soak tests, and a seventeen-mutant safety gate. The complete scope and exclusions are in [`OFFLINE_BEHAVIOR_TESTS.md`](OFFLINE_BEHAVIOR_TESTS.md). The incident-derived layer is documented in [`PRODUCTION_INCIDENT_REPLAY_TESTS.md`](PRODUCTION_INCIDENT_REPLAY_TESTS.md).

The original v4.0.0 source tree (before the ATR checkpoint saving correction) passed **1,284/1,284** pytest cases across **129 test modules**, with `ResourceWarning` promoted to an error. All 129 modules also passed individually in fresh Python processes. The measured combined statement/branch coverage was **78.8%** (82.3% statements; 68.1% branches), and **1,038/1,038** executable application callables were entered. The release killed **17/17** safety mutants and passed **58/58** deterministic simulation contracts across 54 CSV paths. The three new v4.0.0 modules contain **73 focused regression cases**. Ruff, Pyright, native Windows/PyInstaller, and live IBKR testing were not available in this environment.

### Build-script tests

Verify launch/build script behavior, including:

- process-local PowerShell policy bypass;
- environment cleanup;
- pause/exit-code propagation;
- required test/quality stages;
- PyInstaller output handling and integer exit-code return.

## Deterministic CSV simulations

`tests/simulated_data/` contains 54 fixed price-path files. `tests/simulation_scenario_catalog.py` binds those files to 58 explicit scenario contracts, and `scripts/run_all_simulations.py` executes every contract in one process. A catalog-integrity check fails when a CSV is unregistered, missing, malformed, non-finite, or contains an invalid time/RTH/slippage value.

Each contract states the expected final stage and exact event sequence. Depending on the scenario, it also checks planned and filled quantities, selected trigger/fill prices, order payload values, P/L direction and formula, minimum-profit preservation, budget exposure, error messages, and RTH behavior. Shared invariants reject overfills, negative quantities, duplicate lifecycle events, SELL-before-BUY ordering, incorrect application order references, non-maximal whole-share sizing, and inconsistent completed-cycle P/L.

The current matrix covers anchor/drop boundaries, BUY and SELL trail boundaries and ratchets, gaps through stops, partial fills, zero-trailing market orders, protective exits, RTH closure/reopen transitions, slippage and sizing buffers, reinvestment, low/high-price quantity edges, decimal rounding, and longer no-order/working-order paths.
The complete named catalog is in [`CSV_SIMULATION_SCENARIO_MATRIX.md`](CSV_SIMULATION_SCENARIO_MATRIX.md).

The simulator intentionally models the application’s strategy rules, not the full IBKR execution engine. It does not reproduce:

- exchange queue priority;
- every TWS trigger-method nuance;
- gap/slippage distributions;
- latency, rejection, account permissions, or buying power;
- real market-data field availability;
- live disconnect/reconnect timing.

Use simulations to detect deterministic rule regressions, not to estimate profitability.

## Ruff

Configured in `pyproject.toml` with a conservative rule set focused on:

- syntax/Pyflakes failures;
- selected likely-bug checks;
- import ordering;
- a small number of obvious simplifications.

Broad formatting rewrites are intentionally not part of the gate.

## Pyright

Configured in basic mode for the core model, strategy, and storage modules. The `pyright[nodejs]` dependency supplies a Node runtime where the wheel supports it. Type scope is intentionally narrower than the full Qt/controller codebase.

## Windows packaging validation

The default build command:

```powershell
.\build_windows.bat
```

skips tests for faster packaging. Use:

```powershell
.\scripts\build_windows.ps1 -RunTests
```

when pytest and simulations should run before PyInstaller. That switch runs plain pytest and the CSV simulations only; it does not run the coverage threshold, callable gate, mutation checks, Ruff/Pyright, or the full launcher's ResourceWarning policy. Run `run_all_tests.bat` for the complete gate before packaging.

A successful build requires both a zero PyInstaller exit code and the expected executable at `dist\IBKRTradingBot\IBKRTradingBot.exe`.

## Live integration testing

Automated tests cannot prove end-to-end broker behavior. Before live deployment, execute the manual plan in [`TEST_PLAN.md`](TEST_PLAN.md), beginning with an IBKR paper account and observing actual TWS/Gateway order fields, fills, cancellations, and recovery.

## v4.0.0 regression layer

`test_v400_atr_memory_and_order_edits.py` covers first-session warmup, weekend/restart reuse, session and contract separation, corrupt/future/expired estimates, bounded persistence failures, live takeover, same-contract volatility-history preservation, and explicit next-order guard persistence/isolation. `test_v400_gui.py` checks amber LIVE status, retained error colors, removal of only the profit banner, risk-field/manual locks, saved ATR provenance, and non-selling dialog defaults. `test_v400_release.py` checks metadata, documentation layout, compatibility, and unchanged order/broker modules.

Historical corrected v4.0.0 results are preserved in [`legacy/V4_0_0_IMPLEMENTATION_TEST_REPORT.txt`](legacy/V4_0_0_IMPLEMENTATION_TEST_REPORT.txt). The root `IMPLEMENTATION_TEST_REPORT.txt` records the current v5.3.0 verification.


## v5.0.0 GUI regression layer

The retained GUI regression suites cover fill/commission-derived costs, remaining-inventory allocation, invalid/missing data, USD/EUR formatting, five merged monitor cards, collapsed stage details, Simple-mode log visibility, exact-copy wrapping OrderRefs, twelve summary cards, numeric Invested sorting, stable row identity, bounded table height, expanding Decision events and equal Timeline table heights. Current checks cover Trading/Position details in the top-right header with the title font, and independent Timeline crosshairs with separate price scales. Hovering one plot must not draw a cursor on the other; offscreen records must not replace a local tooltip. Table regressions cover all eight wrapped Reconciliation rows, width/font/style/data-triggered refitting, the two-by-two guided-action layout, horizontal-scrollbar height allowance, and reachable overflow in height-capped Summary/compact tables. Native Windows DPI rendering remains a separate platform check; the root report records the current native and headless results. `tests/test_v410_release.py` checks current release consistency, retained documentation and unchanged safety boundaries. The full v5.0.0 release also includes targeted controller, strategy, broker adapter, model and storage safety fixes.

The current measured suite, coverage and quality-gate results are in [`../IMPLEMENTATION_TEST_REPORT.txt`](../IMPLEMENTATION_TEST_REPORT.txt). Qt doubles verify observable layout policies and painter coordinates, not native pixel geometry. Native Windows/DPI, a packaged build, and the actual GUI scrollbar/crosshair interaction still require the visual checks in `TEST_PLAN.md`.

## v5.0.0 safety regression layer

`tests/test_v500_*.py` covers broker submission uncertainty, cancellation ownership, market-data generations, recovery quantities/late fills, account/contract identity, independent BUY evidence/spread limits, protective handoff/precision, restore validation, GUI eligibility, Python 3.14 launcher checks, audit-reader lifecycle and batching, validated post-fill capture context, actual stage transitions, disconnected Account display and metadata-only RTH handling. New unittest classes are also pytest-collectable. Real pytest/coverage/quality gates remain required; any dependency-free fallback results in the implementation report are explicitly separate and do not replace them.

## v5.1.0 targeted regression layer

The new unittest-compatible modules cover session/profile identity and scoped manual confirmation (`test_v510_gui_commands`), recovery ownership and signed/currency-checked commissions (`test_v510_recovery_commissions`), committed checkpoint preservation (`test_v510_checkpoint`), cancel-confirm-market-close transitions and restart (`test_v510_manual_close`), quote consumption/monotonic ages/final BUY checks (`test_v510_data_timing`), and completion-date/ATR-edit guards (`test_v510_strategy_risk`). They exercise temporary databases, controlled timing and broker doubles, including successful adjacent paths.

`test_v510_lock_button_geometry` uses the real Qt layout engine in isolated subprocesses at three scale factors, both themes, two widths and both lock states. It explicitly skips when PySide6 is absent. The fixed review host cannot turn that skip into a visual/DPI pass. The root verification report gives the executed results and external Windows/Python 3.14 gates; focused counts overlap and must not be summed.
