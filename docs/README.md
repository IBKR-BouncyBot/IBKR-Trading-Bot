# Documentation index

The files in this directory describe the current v3.0.18 behavior unless explicitly marked otherwise. The root of `docs/` is intentionally limited to current operating, design, recovery, and verification material. Superseded release notes are stored under [`legacy/`](legacy/README.md).

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
| [`TEST_PLAN.md`](TEST_PLAN.md) | Manual verification checklist, especially for Windows and IBKR integration |
| [`CODE_REVIEW_NOTES.md`](CODE_REVIEW_NOTES.md) | Maintainer review boundaries and documentation-maintenance rules |

## Current release note

[`V3_0_18_EVENT_DRIVEN_CADENCES.md`](V3_0_18_EVENT_DRIVEN_CADENCES.md) records the v3.0.18 event-driven scheduling, nonblocking scheduled broker reads, independent responsibility cadences, and verification results.

## Archived documentation

The [`legacy/`](legacy/README.md) directory contains v3.0.17 and earlier release-specific notes. Those files may accurately describe the release that introduced a feature, but labels, defaults, layouts, tests, and limitations in them can be obsolete. They are not the current operating specification.
