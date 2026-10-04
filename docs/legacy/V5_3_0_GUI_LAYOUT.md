# v5.3.0 GUI layout

Version 5.3.0 changes the desktop presentation and release metadata. The trading strategy, broker integration, market-data handling, storage behavior, backup policy and worker timers retain their 5.2.0 behavior. The targeted database index and removal of the redundant temporary backup copy remain in place.

## Ticker and Live strategy layout

When no ticker name is available, the top Ticker box reads **N/A** instead of **Waiting for ticker confirmation / SMART / USD**. Populated ticker labels keep their existing identity formatting. This is a display placeholder and does not change contract confirmation or qualification.

In **Simple**, **Advanced** and **Debug**, the **Price data monitor** contains the **Market and strategy graph** directly below **Strategy progress** and above the five metric boxes; the separate graph area and the “Rolling graph buffer…” footer are removed. In Advanced and Debug, the connection/strategy configuration row comes first, followed by Price data monitor directly above **Market and strategy state**. Simple hides the configuration row, leaving Price data monitor as the first visible panel. Existing graph history, updates, plot data, monitor fields and other view-mode visibility rules are retained.

Engaging the GUI lock hides the entire bottom command bar on Live strategy: the five workflow buttons and the view-mode selector. Editable configuration remains locked. The top lock and tab navigation remain accessible, and market/strategy monitoring continues. Unlocking restores the command bar with its previous view mode and the existing state-dependent button permissions. Unlocking does not independently authorize a blocked action.

The Simple-mode guidance ends with “Stage details are expandable.”

## Strategy flowchart

The five stage cards share a compact height calculated from the largest text requirement across all five stages. Fonts keep their existing sizes. Measuring the full card set keeps the heights consistent when switching between full and filtered flowchart views; longer content or a narrower available width can increase the common height so text remains readable.

The flowchart remains read-only. Stage descriptions, settings, historical-cycle selection, active-state highlighting and trading calculations are unchanged.

## Reconciliation navigation

A native **Reconciliation** button occupies the top-right corner of the tab row, while Live strategy, Strategy flowchart and Trade history stay at the left. The button opens the existing Reconciliation page and indicates when that page is selected. Only its former left-side tab header is hidden; the four page indices, page contents, actions and safety gates are retained. Keyboard users can focus the Reconciliation button with Tab and activate it with Space. Ctrl+Tab cycles the three visible left-side tabs and skips the hidden Reconciliation tab header.

The three numbered Reconciliation step headings use regular font weight in both light and dark themes. Their wording, size and actions are unchanged.

## Compatibility and release files

No database migration or dependency change is introduced by this release. Standard GIL-enabled CPython 3.14.x remains required by the source launch, test and build scripts.

1. Close the instance before replacing its application files. Keep its database, settings, captures and backups.
2. Use the complete `IBKR_Trading_Bot_5.3.0_Source.zip`, or apply the cumulative `IBKR_Trading_Bot_5.3.0_Release_files.zip` overlay to the delivered 5.2.0 or preceding 5.3.0 source baseline identified in its instructions. The included patch is only for the exact 5.2.0 baseline. The archive name replaces the former `Updated_Files.zip` convention.
3. Run `run_all_tests.bat` on the target Windows environment. Rebuild an existing executable from the new source; copying Python files does not update a packaged executable.

## Verification boundaries

The focused regression modules cover:

| Module | Scope |
|---|---|
| `tests/test_v530_live_layout.py` | Empty/populated ticker display, embedded graph position, footer removal and monitor ordering in all view modes, lock/unlock visibility and retained command gating. |
| `tests/test_v530_flowchart_layout.py` | Shared card height, text measurement, retained fonts, width/content changes and filtered views. |
| `tests/test_v530_reconciliation_tab.py` | Right-side selector, preserved page indices, opening the existing page and selected-state synchronization. |

Release consistency checks cover the version and current documentation. Retained strategy, recovery and storage tests check for unintended changes outside the requested presentation scope.

The root [implementation and test report](V5_3_0_IMPLEMENTATION_TEST_REPORT.txt) records the tests actually run, their results and any remaining native Qt, Windows, DPI or build checks. Test definitions and headless widget checks alone do not establish native rendering results. The [manual test plan](../TEST_PLAN.md) includes resize, theme, scaling, keyboard/navigation and lock checks. No live broker order is needed for these GUI checks.

The [archived 5.2.0 release note](V5_2_0_STORAGE_PERFORMANCE.md) and [verification report](V5_2_0_IMPLEMENTATION_TEST_REPORT.txt) preserve the preceding storage changes and their measured scope.
