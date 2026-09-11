# v3.0.18 Event-driven controller cadences

## Summary

v3.0.18 removes two fixed waits from scheduled runtime work and separates controller responsibilities onto independent monotonic cadences:

- broker callbacks and connectivity: 50 ms;
- strategy evaluation and periodic order-state polling: 100 ms;
- GUI snapshot emission: 500 ms;
- read-heavy database snapshot data: 1 second; and
- maintenance work: 1 second.

The worker now waits interruptibly on its command queue until the earliest scheduled deadline. A newly queued GUI command wakes the worker immediately rather than waiting behind a fixed one-second sleep.

## Market-data and order-state behavior

Scheduled strategy quote reads use a zero timeout. The adapter first inspects the existing subscription snapshot and returns immediately when data is available. Operations that explicitly permit a positive timeout wait only while data is absent and use slices no longer than 50 ms; changing the TWS market-data type no longer inserts an unconditional 250 ms sleep.

Periodic order polling is also nonblocking. It returns cached trade state immediately. On a cache miss it may request a throttled open-order refresh, but the scheduled strategy path consumes the result on a later broker cadence instead of synchronously waiting.

## Database and maintenance separation

GUI snapshots read from a database-display cache refreshed on the database cadence. Recent events, history summaries, and display-only guard or position facts are not queried on every GUI render. Human-readable diagnostic-report generation and stale-cycle housekeeping run on the maintenance cadence.

Safety-critical persistence remains synchronous, including order intent, broker identifiers, fills, cancellations, strategy transitions, reconciliation, recovery, and shutdown checkpoints. Final BUY authorization continues to query live SQLite risk and position facts; display cache contents cannot authorize an order.

## Threading and shutdown boundary

Broker event handling, command dispatch, strategy evaluation, and critical persistence remain serialized on the original controller worker thread. No parallel broker-adapter access was introduced. Broker callbacks and connectivity are processed before strategy evaluation, and an unready broker cycle prevents that strategy cycle.

After the stop event is set, shutdown preempts an older queued command so a pending broker action is not executed during teardown.

## Versioning

The GUI title and built-in example-data notice, Python package metadata, Windows release-builder metadata, current documentation, and version-regression tests identify this release as v3.0.18. The v3.0.17 release note is retained under `docs/legacy/`.

## Verification

The complete unfiltered collection executed all 828 pytest cases: 827 passed and one strict documented expected failure. The repository gate passed with 823 non-soak cases, five bounded soak tests, 76.3% combined statement/branch coverage, entry into all 822/822 effective executable application callables, 6/6 safety mutants killed, and all 58 deterministic CSV contracts passing across 54 price-path files. Detailed commands and external-integration limits are recorded in `IMPLEMENTATION_TEST_REPORT.txt`.
