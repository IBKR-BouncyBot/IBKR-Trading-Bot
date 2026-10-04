# v5.5.1 completed-trade summary and history filters

Version 5.5.1 corrects the Completed trade summary after an instance changes its strategy ticker. The summary now follows the filters in **Trade history**, rather than the ticker configured for the next strategy cycle. Stored trades and their recorded amounts are retained.

## Summary scope

| Trade history selection | Completed trade summary |
|---|---|
| Ticker blank and all other filters cleared | All completed cycles for every ticker in this portable database. |
| A ticker filter entered | All completed cycles matching that ticker filter and the other selected filters. |
| Date, outcome, ATR or Paper/live filter selected | All completed cycles matching that same combination of history filters. |
| No completed cycle matches | Empty or zero summary metrics, according to the existing metric format. |

The table displays the latest **500 matching rows**. Filtering is applied before this display limit, so an older matching row is no longer excluded just because 500 newer nonmatching rows exist. The summary covers **all matching completed cycles**, including those beyond the displayed 500. The built-in synthetic example shown for an empty unfiltered history is excluded from the summary totals.

The existing date-filter basis is retained: the first recorded value from SELL fill time, BUY fill time, update time or creation time. These are normally recorded in UTC. Ticker matching and the meanings of outcome, ATR and Paper/live choices are retained.

Filter changes request matching history data on the existing controller worker. The GUI checks which filters a result belongs to before accepting it; a late result for an earlier filter cannot replace the current selection's results. The existing database snapshot cadence and summary cache are retained. Use **Refresh** to reload history rows after database contents change; this release does not add automatic table refresh after each fill.

This is local application history, not account-wide performance. Each portable database has one contract currency; the summary does not perform foreign-exchange conversion or combine databases from different bot instances. **Export CSV** retains its existing export scope and is not changed to mirror every GUI filter by this release.

## Scope and upgrade

The correction affects history filtering, read-only queries and their GUI/worker request path. It adds no schema migration, database write, persisted setting, broker command or strategy rule. Connection/freshness presentation introduced in 5.5.0 is retained, as are order handling, RTH/ATR safeguards, timer intervals and backup policy.

1. Close the instance before replacing files. Preserve its database, settings, captures and backups.
2. Use `IBKR_Trading_Bot_5.5.1_Source.zip` or apply `IBKR_Trading_Bot_5.5.1_Release_files.zip` to the 5.5.0 baseline described in that archive. Keep each instance's own directory, database and client ID.
3. Run `run_all_tests.bat` in the target Windows environment. Standard GIL-enabled CPython 3.14.x remains required. Rebuild an existing packaged executable from the updated source; replacing Python files alone does not update that executable.
4. Open Trade history and clear its filters to review the complete database totals. Enter a ticker to inspect only its matching history.

## Verification boundaries

`tests/test_v551_history_storage.py` and `tests/test_v551_history_gui.py` cover all-ticker totals, explicit filters, completed-only metrics, histories exceeding 500 rows and filter/result identity. The retained regression suite covers the surrounding history and trading behavior. The root [implementation and test report](V5_5_1_IMPLEMENTATION_TEST_REPORT.txt) records the exact executed checks and their results.

This host provides Python 3.12 and headless Qt doubles. Installing the required Python 3.14/Qt/quality-tool environment was blocked by HTTP 403 responses. Test definitions and headless results do not establish a native Windows/Qt rendering check, a Python 3.14 run, Ruff/Pyright completion or live-broker verification. Use the [manual test plan](../TEST_PLAN.md#completed-trade-summary-and-history-filters) for the target-platform display checks.

The accompanying [GUI review](../GUI_REVIEW_5_5_1.md) records additional discrepancies and verification limits. Additional findings are reported for review rather than silently changing unrelated GUI behavior in this patch.

The preceding [5.5.0 GUI status note](V5_5_0_GUI_STATUS.md) and [verification report](V5_5_0_IMPLEMENTATION_TEST_REPORT.txt) remain available as history.
