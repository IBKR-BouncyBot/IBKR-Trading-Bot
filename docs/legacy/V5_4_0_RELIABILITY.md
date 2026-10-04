# v5.4.0 targeted reliability changes

Version 5.4.0 implements the reviewed backup and ATR changes from the supplied patch with narrow safety adjustments. The 5.3.0 GUI layout and dependencies are retained. The README no longer includes the screenshot-age note.

## Deferred order and fill backups

Nine order/fill paths now enqueue a backup request through the existing controller command queue. The current order/fill operation completes before the queued copy runs. Durable order intent, cycle, execution and commission writes remain in their original synchronous paths, as do the final broker/market-data submission guards. Submission requests use the reason `order_submission` because their backups are no longer guaranteed to precede transmission.

There is no new thread. The same controller worker later performs the full copy and restore validation, so backup work can still delay subsequent broker callbacks, strategy work and snapshots. A queued backup captures the database when the copy runs; it is not a snapshot of the exact request time. Lifecycle, shutdown and audit-export backups retain their existing behavior. This change does not promise lower total backup work or eliminate all worker stalls.

The patch's proposed validation bypass is not used. Every ordinary backup retains the existing integrity, required schema/primary-key, foreign-key and disposable migration validation before acceptance and retention.

## Schema-aware startup copies

Successful schema initialization records `PRAGMA user_version = 1`. An existing unstamped or older database retains a best-effort pre-migration online copy; subsequent openings with the current stamp skip that redundant startup copy. The existing idempotent table, column and index checks still run. No application table or column is added by 5.4.0.

The existing database stamp is read through a read-only connection. If it cannot be read, or is above `1`, opening is rejected before backup, migration or database writes. This release does not silently treat an unknown future schema as compatible. The stamp does not substitute for backup validation or broker reconciliation.

## Validated backup retention

The default retention is 20 backup files. Pruning occurs only after a newly created backup passes full restore validation. Opening the app does not prune existing backups. Failed creation or validation also preserves older copies; deletion failures can leave more than 20 files. The pre-migration copy remains best effort and does not itself trigger retention.

The smaller retained set reduces backup disk occupancy after successful rotation. It does not reduce how much an individual full copy writes or introduce a periodic backup interval.

## Saved ATR estimate age

A saved RTH ATR estimate is eligible for reuse only up to 24 elapsed UTC hours after its recorded observation; the exact 24-hour boundary is accepted. Valid same-session or consecutive-day reuse remains possible within that limit. Older checkpoints, including ordinary Friday-to-Monday and longer holiday gaps, fall back to the existing warmup. No exchange holiday calendar is inferred.

All other checkpoint identity, profile, contract, configuration, RTH-boundary, timestamp and value checks remain. A seed does not count as a fresh market-data observation and does not bypass the current quote, session or trading guards. This deliberately tightens the previous seven-day policy without changing ATR formulas or checkpoint write cadence.

## Upgrade and release files

1. Close the instance before replacing files. Retain its database, settings, captures and backups.
2. Use `IBKR_Trading_Bot_5.4.0_Source.zip` or the `IBKR_Trading_Bot_5.4.0_Release_files.zip` overlay according to its included baseline instructions. Preserve separate directories, databases and client IDs for separate instances.
3. The first opening of an unstamped 5.3.0 database creates a best-effort pre-migration copy and stamps the supported schema after checks succeed. Subsequent openings skip that startup copy. Existing application rows remain in place.
4. Run `run_all_tests.bat` in the target Windows environment. Rebuild the executable from the new source; copying Python files does not update a packaged executable. Standard GIL-enabled CPython 3.14.x remains required.

## Verification boundaries

| Coverage | Scope |
|---|---|
| `tests/test_v540_deferred_backups.py` | Queue deferral, backup request reasons, order/fill sequencing and preserved durable state. |
| `tests/test_v540_storage_reliability.py` | Legacy/current/future schema handling and unreadable-stamp rejection, validation retention, backup failure and pruning boundaries. |
| Existing v4.0.0 ATR memory and close-persistence modules | Exact and exceeded 24-hour boundary, consecutive-day and weekend behavior, retained checkpoint guards. |
| Existing storage, order, recovery and simulation layers | Compatibility and unintended changes outside the targeted scope. |

The archived [implementation and test report](V5_4_0_IMPLEMENTATION_TEST_REPORT.txt) records actual execution results and unavailable gates. Test definitions are not evidence that native Qt, Windows, Python 3.14, quality-tool or live-broker checks ran. The [manual test plan](../TEST_PLAN.md) includes the relevant startup, retention and checkpoint cases.

The preceding [5.3.0 GUI release note](V5_3_0_GUI_LAYOUT.md) and [verification report](V5_3_0_IMPLEMENTATION_TEST_REPORT.txt) remain available as history.
