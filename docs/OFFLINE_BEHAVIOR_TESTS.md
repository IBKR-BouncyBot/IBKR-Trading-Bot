# Deterministic offline behavior tests

This document describes the current non-GUI, non-Windows, non-network test layer in v3.0.19. It covers strategy behavior, controller state transitions, broker-event handling, persistence and recovery, shutdown checkpoints, GUI contracts, and bounded performance behavior.

The suite deliberately avoids:

- launching the PySide6 GUI;
- Windows-specific executable, registry, process, DPI, or native-widget behavior;
- a real TWS or IB Gateway process;
- paper or live IBKR accounts;
- real market data, account data, orders, fills, or network traffic.

The tests use temporary SQLite databases, deterministic clocks and prices, protocol-shaped broker doubles, subprocesses, and generated event sequences. They verify the application contract at its internal and adapter boundaries, not the behavior of IBKR's external systems.

## Test layers

### Broker callback replay and permutation

`tests/test_broker_event_replay_permutations.py` replays equivalent order histories with callbacks in different orders. It covers open-order, order-status, execution, and commission events; duplicate execution identifiers; repeated terminal polls; persisted raw events; and idempotent replay.

The primary invariant is that harmless callback ordering and duplicate delivery must not change the final cycle, quantity, commission, or database state.

### Generated controller state sequences

`tests/test_stateful_controller_invariants.py` drives the real controller, strategy engine, and SQLite storage through deterministic generated scenarios. It covers complete and partial BUY/SELL paths, zero and nonzero trails, external positions, app-owned quantities, connectivity failures, retry behavior, and maximum-cycle enforcement.

The assertions include:

- no order transmission while a required guard is active;
- no SELL above app-owned unsold quantity;
- external positions do not become app-owned;
- no extra cycle after the configured maximum;
- completed cycles have no remaining app-owned quantity;
- repeated recovery information is idempotent.

### Generated numerical and malformed-payload properties

`tests/test_generated_numeric_and_payload_properties.py` applies deterministic generated inputs to pricing, quantity, profit-protection, serialization, and broker-payload normalization functions. It includes threshold-adjacent values, large and small finite values, malformed fields, and nonfinite values.

These are property-style tests: they assert invariants across many generated cases rather than checking only one example.

### Recovery and guard decision matrices

`tests/test_recovery_and_guard_decision_matrix.py` exercises combinations of local/upstream connectivity, missing or stale prices, RTH state, ATR readiness, app-owned positions, external positions, and recovery confidence.

The matrix verifies that normal trading guards remain distinct from recovery-required states and that fail-closed behavior is consistent across combinations.

### Differential simulation

`tests/test_differential_simulation_equivalence.py` runs generated price paths through independent simulation paths and compares stage transitions, order actions, trigger behavior, filled quantity, completion, and P/L within the documented rounding tolerance.

The comparison is intended to detect drift between simulation implementations. It does not model exchange queue priority, latency, broker-side trigger details, or real slippage.

### Multi-instance isolation

`tests/test_multi_instance_isolation.py` creates two controllers with separate roots, SQLite databases, client IDs, tickers, local ledgers, backups, and audit output while sharing one deterministic Gateway model.

It verifies normal client-isolated operation. One strict expected-failure test records a known architectural limitation: a Master Client-style event stream can contain an unmatched order from another installation with the same `IBKRBOT` order-reference prefix, because the current prefix does not encode a unique installation identifier. The test is `strict=True`, so an unexpected pass also fails the suite and requires review.

### Crash, restart, migration, and restore

`tests/test_crash_restart_and_migration_matrix.py` uses child processes and abrupt `os._exit()` termination to test committed and uncommitted SQLite boundaries. It also covers zero-byte databases, reconstructed legacy schemas, migration idempotence, corrupt databases, startup backups, and backup validity after an abrupt writer exit.

These tests verify SQLite/application recovery semantics. They do not claim to reproduce every operating-system cache, filesystem, or hardware power-loss behavior.

### Storage fault injection and retention

`tests/test_storage_fault_injection_and_retention.py` injects failures into readable audit logging, restore-copy creation, backup integrity validation, validation-report writing, capture ZIP writing, and backup rotation. It verifies exact retention behavior at multiple limits and checks filename sanitation and fallback exports.

### Gateway outage and stale-data sequences

`tests/test_gateway_fault_injection_sequences.py` generates local and upstream connection failures, codes 1100 and 2110, restoration with and without market-data loss, repeated flapping, stale cached snapshots, recovery failures, and a connectivity race at the final order-placement boundary.

The deterministic assertions verify that:

- cached pre-outage data is never reclassified as fresh;
- ATR and strategy advancement require a new event;
- new orders remain blocked while connectivity or reconciliation is uncertain;
- subscription replacement/retention follows the restoration mode;
- a final connectivity check prevents a late-race order submission.

### Bounded soak tests

`tests/test_accelerated_soak_bounds.py` is marked `soak`. The complete Windows `run_all_tests.bat` gate now runs it inside the same Coverage.py invocation as every other pytest test; the Unix helper retains a separate non-instrumented soak pass. It verifies bounded behavior under:

- 25,000 market-data capture snapshots;
- 2,000 ATR bar buckets;
- 22,500 unique price events plus 1,000 duplicate reads;
- 250 completed persisted cycles;
- 1,000 upstream disconnect/restore cycles.

The tests check configured deque limits, ATR history limits, price-history limits, sequence deduplication, cycle/reinvestment totals, event draining, subscription generations, and thread-count stability. They are deterministic functional soak tests, not hardware performance benchmarks.

### Safety mutation smoke gate

`scripts/run_mutation_smoke.py` copies the application package into temporary directories, applies one deliberate defect at a time, and runs an independent probe. Production sources are never edited in place.

The gate currently requires tests to kill six safety mutations:

1. reversing BUY slippage sizing;
2. excluding an exact BUY trailing-stop boundary;
3. excluding an exact SELL trailing-stop boundary;
4. excluding the exact initial-drop boundary;
5. using the larger instead of overlapping BUY/SELL quantity for P/L;
6. using the wrong completed-exit leg when calculating app-owned unsold quantity.

A surviving mutant fails the validation run.

## Deterministic broker support

`tests/support/deterministic_broker.py` implements the application-visible broker boundary in memory. It provides independent local and upstream connection states, event-stamped market data, order placement/cancellation, partial and terminal fills, execution history, recovery snapshots, account lists, and injected failures.

`tests/support/controller_harness.py` creates controllers with isolated SQLite storage and permissive settings so tests can focus on the behavior under examination.

The doubles intentionally do not import a live IB session, open sockets, or claim to reproduce undocumented server behavior.

## Validation sequence

The complete Windows launcher runs the deterministic layers in this order:

1. Compile `app`, `tests`, `scripts`, and `main.py`.
2. Run every collected pytest test, including the bounded soak tests, with `ResourceWarning` promoted to an error and Coverage.py branch tracking enabled. No marker filter is applied.
3. Enforce the 75% combined statement/branch threshold.
4. Write `coverage.json` and `coverage.xml`.
5. Require entry into every effective executable application callable.
6. Run the six-mutant safety smoke gate.
7. Run all deterministic CSV simulations.
8. Run Ruff and Pyright through `run_all_tests.bat`.

The Unix `scripts/run_tests.sh` helper still separates non-soak coverage from the soak subset to keep that development-host command practical.

The current v3.0.19 validation inventory is:

- 832 non-soak tests: 831 expected passes and 1 strict expected failure documenting the Master Client/shared-prefix limitation;
- 5 bounded soak tests;
- 835/835 effective executable application callables entered;
- 77.0% measured combined statement/branch coverage against the 75% minimum;
- 6/6 safety mutants killed;
- 58 validated CSV scenario contracts across 54 price-path files.

The CSV matrix itself passes cleanly in the offline Linux environment. The full Windows launcher remains the authoritative combined run for Coverage.py, Ruff, Pyright, and native launcher behavior.

## Scope that remains external

The following still require separate environments and are intentionally not part of this offline layer:

- actual PySide6 widget rendering and user interaction;
- native Windows locking, process, DPI, and packaged-executable behavior;
- a real TWS/IB Gateway handshake and callback stream;
- paper-account or live-account order lifecycle tests;
- real market-data entitlements, pacing, fills, commissions, margin, and exchange behavior;
- true Internet interruption between Gateway and IBKR;
- hardware-specific latency, CPU, memory, and endurance qualification.

Use [`TEST_PLAN.md`](TEST_PLAN.md) for those manual and integration checks.
