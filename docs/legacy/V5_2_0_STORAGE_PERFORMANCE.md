# v5.2.0 targeted storage performance changes

Version 5.2.0 implements two storage changes identified by the CPU and disk investigation: an index for manual-handling lookups and removal of a redundant full temporary database backup during restore validation. Runtime behavior changes are limited to `app/storage.py`; release metadata identifies the new version.

## Manual-handling lookup index

Startup creates this index if it is missing:

```sql
CREATE INDEX IF NOT EXISTS idx_decision_events_manually_handled_cycle
ON decision_events(cycle_id) WHERE event_type = 'MANUALLY_HANDLED';
```

The existing unresolved-cycle and app-owned-position queries check these markers when excluding explicitly acknowledged historical cycles. The index lets SQLite find the relevant marker without scanning unrelated decision events for that cycle. Query predicates, result ordering, legacy error-message exclusions and acknowledgement semantics are unchanged. Trading preflight and recovery continue to read the database at their existing boundaries; this change does not substitute cached authorizations.

The index contains only `MANUALLY_HANDLED` entries. Its first creation scans the decision-event table and writes the index; later normal decision events of other types do not add entries to it. First startup can therefore take longer while the missing index is built. The change adds no table or column and modifies no application table row.

## Restore validation without a redundant backup

Normal backup creation still uses SQLite's online backup API. Restore validation still checks the supplied backup, creates a consistent disposable SQLite candidate including committed WAL contents, opens that candidate through additive migrations, validates it, and removes the temporary directory.

Previously, opening that disposable candidate also created a full pre-schema backup of the candidate inside the temporary directory. That extra backup was discarded with the candidate after validation. Version 5.2.0 skips only this internal backup, eliminating one full database-sized temporary copy per successful validation workflow. The actual backup and consistent restore candidate remain.

Opening an existing real database retains its default pre-schema backup. Validation does not migrate the supplied backup or active database. Failure reporting and acceptance checks remain in place. Existing backup triggers, names, default retention of 50 and rotation are retained. Full backups remain event-triggered; no periodic backup timer or minimum backup interval is introduced.

## Compatibility and upgrade

Existing databases use the normal additive startup path. The partial index preserves all stored settings, cycles, orders, executions, acknowledgements and ATR checkpoints. Upgrades from releases earlier than 5.1.0 also run their existing migrations, including the completion-date migration documented in [Database schema](../DATABASE_SCHEMA.md#v510-completion-date-migration).

1. Keep the working installation and a consistent database backup. Close the instance before replacing application files.
2. Use the complete 5.2.0 source, or apply the release patch only to its stated 5.1.0 baseline. Keep each instance's own database and client settings.
3. Source launch, test and build continue to require standard GIL-enabled CPython 3.14.x and the existing dependency ranges. There is no dependency upgrade in this release.
4. Run `run_all_tests.bat` on the target Windows environment. Building from the updated source with `scripts/build_windows.ps1 -RunTests` produces the new executable; replacing source does not update an existing executable.

## Verification and measurement boundaries

| Regression module | Coverage |
|---|---|
| `tests/test_v520_manual_marker_index.py` | Fresh and existing database opens, repeated opening, application-row preservation, ordered unresolved-cycle and position results, every stage, working/terminal/unknown order states, contract separation, manual and legacy acknowledgements, commit/rollback visibility, partial-index contents and use by the unchanged read-only queries. |
| `tests/test_v520_backup_validation.py` | Actual SQLite copy counts, ordinary pre-schema backup preservation, legacy candidate migrations, committed WAL contents, source/backup/candidate row preservation, injected copy/migration/integrity failures, temporary cleanup, retention and audit export. |

These tests use temporary synthetic databases and do not connect to a broker or transmit orders. Failed candidate validation must still reject the new backup and preserve existing recovery points.

The archived [implementation and test report](V5_2_0_IMPLEMENTATION_TEST_REPORT.txt) records the checks actually executed, their results, the release source scope and any remaining platform gates. It is the authoritative record for this release's measured test and performance results.

The preceding investigation profiled copied databases and synthetic histories on Linux using CPython 3.12, not the live Windows process. Those measurements established the targeted lookup and extra copy as reducible work; they do not establish a whole-app CPU reduction or physical SSD-write rate for this release. The benefit depends on the current strategy stage, historical decision volume and backup events. A waiting Stage 3 cycle does not incur the more expensive Stage 1 position lookup, and backup-copy savings occur only when validation runs.

## Retained behavior

The five-stage strategy, broker orders, order ownership, market data, RTH and ATR handling, recovery checks, immediate order/fill persistence and SQLite durability settings retain their 5.1.0 behavior. Worker, GUI, database, watchdog and diagnostic timers are unchanged. This release does not implement duplicate-write suppression, warning coalescing changes or slower display refreshes.

The [archived 5.1.0 release note](V5_1_0_TARGETED_TRADING_FIXES.md) records the preceding trading correctness fixes and lock-button sizing change.
