# Documentation index

The files in this directory describe the current v3.9.0 behavior unless explicitly marked otherwise. The root of `docs/` is intentionally limited to current operating, design, recovery, and verification material. Superseded release notes are stored under [`legacy/`](legacy/README.md).

When documents disagree, use this source-of-truth order:

1. executable source and current automated tests;
2. the root [`README.md`](../README.md) and current guides below;
3. the current release entry in [`CHANGELOG.md`](../CHANGELOG.md);
4. archived notes only for implementation history.

## Project-level documents

| Document | Purpose |
|---|---|
| [`../README.md`](../README.md) | Project overview, setup, operation, data handling, and support boundaries |
| [`../CHANGELOG.md`](../CHANGELOG.md) | Consolidated release history |
| [`../SECURITY.md`](../SECURITY.md) | Private vulnerability reporting and sensitive-artifact guidance |
| [`../LICENSE`](../LICENSE) | PolyForm Noncommercial License 1.0.0 terms |

## Current guides

| Document | Purpose |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Component boundaries, threading, ownership, timekeeping, and data flow |
| [`CONFIGURATION_REFERENCE.md`](CONFIGURATION_REFERENCE.md) | Connection and strategy settings, defaults, and applicability |
| [`STRATEGY_RULES.md`](STRATEGY_RULES.md) | Five-stage strategy rules and formulas |
| [`ORDER_FLOW.md`](ORDER_FLOW.md) | Broker-order lifecycle, ownership, fills, and cancellation ordering |
| [`RISK_CONTROLS.md`](RISK_CONTROLS.md) | BUY blockers, exit behavior, and risk-control semantics |
| [`OPERATIONS.md`](OPERATIONS.md) | Startup, monitoring, stopping, shutdown, and data-retention procedures |
| [`RECOVERY_AND_FAILSAFE.md`](RECOVERY_AND_FAILSAFE.md) | Recovery model and operator actions after interruption or mismatch |
| [`WORKER_WATCHDOG_AND_AUTO_RECOVERY.md`](WORKER_WATCHDOG_AND_AUTO_RECOVERY.md) | Worker/storage supervision, full-process replacement, exact-cycle auto-resume gates, and limits |
| [`RECOVERY_AND_GUARDRAILS.md`](RECOVERY_AND_GUARDRAILS.md) | Technical invariants and fail-closed guard behavior |
| [`DATABASE_SCHEMA.md`](DATABASE_SCHEMA.md) | SQLite tables, ownership, migrations, backups, and exports |
| [`STRATEGY_FLOWCHART_TAB.md`](STRATEGY_FLOWCHART_TAB.md) | Meaning and limits of the GUI flowchart view |
| [`LIMITATIONS.md`](LIMITATIONS.md) | Explicit non-goals, platform limits, and distribution boundaries |
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) | Common connection, data, guard, recovery, test, and build issues |

## Verification and maintenance

| Document | Purpose |
|---|---|
| [`TESTING_AND_SIMULATION.md`](TESTING_AND_SIMULATION.md) | Automated validation and quality gates |
| [`CSV_SIMULATION_SCENARIO_MATRIX.md`](CSV_SIMULATION_SCENARIO_MATRIX.md) | Deterministic price paths, expected outcomes, and coverage categories |
| [`AUTOMATED_TEST_COVERAGE.md`](AUTOMATED_TEST_COVERAGE.md) | Per-module callable coverage, test layers, artifacts, and gate semantics |
| [`OFFLINE_BEHAVIOR_TESTS.md`](OFFLINE_BEHAVIOR_TESTS.md) | Replay, generated-state, crash, fault, soak, mutation, and isolation tests |
| [`PRODUCTION_INCIDENT_REPLAY_TESTS.md`](PRODUCTION_INCIDENT_REPLAY_TESTS.md) | Sanitized production-incident replays, privacy controls, historical migration corpus, and resolved-incident regressions |
| [`TEST_PLAN.md`](TEST_PLAN.md) | Manual verification checklist, especially for Windows and IBKR integration |
| [`CODE_REVIEW_NOTES.md`](CODE_REVIEW_NOTES.md) | Maintainer review boundaries and documentation-maintenance rules |

## Current release note

[`V3_9_0_AUDIT_DIAGNOSTIC_COALESCING.md`](V3_9_0_AUDIT_DIAGNOSTIC_COALESCING.md) records stable diagnostic condition keys/reason codes, bounded audit summaries, recovery events, reconnect/native-order wait aggregation, Price Data Monitor guard status, compatibility, and regression scope. The detailed current watchdog contract remains in [`WORKER_WATCHDOG_AND_AUTO_RECOVERY.md`](WORKER_WATCHDOG_AND_AUTO_RECOVERY.md).

## Archived documentation

The [`legacy/`](legacy/README.md) directory contains all superseded release-specific notes and retained historical reports. Only the current v3.9.0 release note remains in the root of `docs/`. Archived files may accurately describe the release that introduced a feature, but labels, defaults, layouts, tests, and limitations in them can be obsolete. They are not the current operating specification.
