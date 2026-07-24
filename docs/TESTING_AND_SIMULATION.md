# Testing, simulation, and quality gates

The repository includes pure-model tests, controller/storage integration tests, protocol-shaped broker-boundary tests, generated event/state tests, crash and fault-injection tests, bounded soak tests, mutation smoke tests, headless GUI component tests, deterministic CSV simulations, and Windows build-script checks. The automated suite does not require a live IBKR session. The current module-by-module coverage map and gate semantics are documented in [`AUTOMATED_TEST_COVERAGE.md`](AUTOMATED_TEST_COVERAGE.md).

## Windows full validation

Run from the project root:

```powershell
.\run_all_tests.bat
```

The launcher:

1. creates/reuses `.venv` through `scripts/run_tests.ps1`;
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

After dependencies are installed:

```powershell
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

That shell script runs compilation, non-soak pytest coverage, the statement/branch threshold, the per-callable gate, bounded soak tests, the mutation smoke gate, and CSV simulations. It does not perform Ruff, Pyright, or Windows PyInstaller packaging and does not replace `run_all_tests.bat` as the complete Windows gate.

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

The non-GUI offline expansion adds broker callback permutations, generated controller invariants, numerical/payload properties, recovery decision matrices, differential simulation, multi-instance isolation, subprocess crash/restart tests, schema migration and restore fixtures, storage fault injection, Gateway outage sequences, bounded soak tests, and a six-mutant safety gate. The complete scope and exclusions are in [`OFFLINE_BEHAVIOR_TESTS.md`](OFFLINE_BEHAVIOR_TESTS.md).

The current v3.1.2 inventory contains 916 non-soak tests, five bounded soak tests, and 58 validated CSV scenario contracts across 54 price-path files. The complete collection contains 921 pytest cases. The final repository gate reports 77.6% combined statement/branch coverage, entry into all 891/891 effective executable application callables, 6/6 safety mutants killed, and all 58 simulation contracts passing.

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

when pytest and simulations should run before PyInstaller. The build script does not run Ruff/Pyright through that switch; run `run_all_tests.bat` for the full gate.

A successful build requires both a zero PyInstaller exit code and the expected executable at `dist\IBKRTradingBot\IBKRTradingBot.exe`.

## Live integration testing

Automated tests cannot prove end-to-end broker behavior. Before live deployment, execute the manual plan in [`TEST_PLAN.md`](TEST_PLAN.md), beginning with an IBKR paper account and observing actual TWS/Gateway order fields, fills, cancellations, and recovery.
