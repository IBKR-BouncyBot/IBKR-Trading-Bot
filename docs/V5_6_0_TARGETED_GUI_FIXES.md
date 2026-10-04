# v5.6.0 targeted GUI corrections

Version 5.6.0 addresses the fifteen findings documented in the [5.5.1 GUI review](GUI_REVIEW_5_5_1.md). It also updates the six support addresses in **About > Info > Thank me** and the root README. The review remains available as the record of the original defects; the corrections below describe the new behavior.

## Changes by review finding

| Finding | Correction |
|---|---|
| G01 | A queued ATR snapshot cannot replace a percentage that has since been switched to manual control. Adaptive input updates respect the current master and per-field adaptation selections. |
| G02 | **Sell app-bought unsold position** in Reconciliation requires the same market-SELL/potential-loss confirmation used by the Stop workflow, with Cancel as the default. |
| G03 | Reconciliation compares broker shares against the full app-owned unsold quantity. A one-share position cannot satisfy a larger local requirement. |
| G04 | A partial fill does not by itself mean an order is finished. Nonterminal partially filled orders remain working in Reconciliation. |
| G05 | After a flat completed/stopped cycle, confirming another ticker shows the new instrument consistently and separates its chart history from the previous instrument. |
| G06 | Estimated trailing-stop graph values use prices from the applicable order's lifetime. Earlier cycle highs/lows cannot move that order's displayed estimate. |
| G07 | A selected historical flowchart remains attached to its cycle ID when history rows are refreshed, inserted or reordered. |
| G08 | Historical flowcharts use the selected cycle's saved settings rather than current ATR, repetition, reinvestment and guard choices. Missing legacy settings use fixed defaults, with the missing fields disclosed in the explanation and tooltip. |
| G09 | With an inactive cycle and manual protection selected, the protective percentage is available as a next-cycle draft under the existing lock/edit permissions. It does not modify that inactive cycle or its broker orders. |
| G10 | CSV export applies the same ticker, date, outcome, ATR and Paper/live filters as Trade history, without the table's 500-row limit. The CSV column schema is retained. |
| G11 | A cancelled, unfilled protective-order reference no longer labels an ordinary completed SELL as a protective exit. Classification requires execution evidence. |
| G12 | Timeline's aggregate BUY/SELL completion markers use the weighted average price for the completed fills. Individual execution records retain their own prices. |
| G13 | Valid zero-valued saved percentages remain visible in Cycle audit Summary instead of being replaced by another field or a missing-value display. |
| G14 | Cycle audit Summary reads the available order-type and timing fields, including broker-style field names, before reporting missing details. |
| G15 | Completed-history changes refresh the history rows and flowchart choices as well as the summary at the existing database snapshot cadence. Unchanged revisions do not trigger repeated full history reads. |

The support-address labels are Cardano / ADA, Ethereum / ETH, Midnight / NIGHT, Solana / SOL, XRP and Zcash / ZEC. The supplied strings are copied exactly; no wallet ownership or network-transfer verification is implied.

## Retained boundaries

Clearing the ticker and all other history filters still includes every completed cycle in that instance's database. The table displays at most 500 matching rows; the summary and CSV include all matching completed cycles. Totals remain local to one portable database and its contract currency; this release does not combine bot instances or perform FX conversion.

These changes address display, editable-input ownership, confirmation and reporting paths. Broker order authorization, the five-stage strategy calculations, RTH/freshness guards, native order submission/cancellation sequencing, database schema and backup policy remain under the existing contracts. A chart's estimated trail is diagnostic and is not an authoritative broker stop or a guaranteed fill price. A confirmation dialog does not override worker-side eligibility checks.

After a restart or when the order's start cannot be established, a trailing-stop estimate remains unavailable unless the necessary order-lifetime price evidence exists. A pre-order cycle extreme or an unobserved broker ratchet must not be presented as a known stop.

Completed-history refresh reuses the existing summary metadata query and an in-memory revision for completed-cycle writes. The revision also detects late commission updates within the same timestamp second. It adds no recurring SQL query or disk write. A completed-cycle update can refresh a filtered view once even if that cycle belongs to another ticker; unchanged subsequent snapshots reuse the existing rows. Older cycle records do not persist every setting, including `auto_repeat`, `sec_type` and `tif`; historical flowcharts disclose defaults for such missing fields instead of silently borrowing the current draft.

## Upgrade

1. Close the application before replacing source files and preserve its database, settings, captures and backups.
2. Use `IBKR_Trading_Bot_5.6.0_Source.zip`, or apply `IBKR_Trading_Bot_5.6.0_Release_files.zip` to the exact 5.5.1 baseline named in that archive.
3. Keep standard GIL-enabled CPython 3.14.x and the unchanged project requirements. Run `run_all_tests.bat` in the supported Windows environment.
4. Rebuild packaged executables from the updated source. Copying Python files into an old executable's directory does not update that executable.
5. Check the affected GUI workflows using the [manual test plan](TEST_PLAN.md#targeted-gui-corrections-in-560), including native Windows scaling and both themes.

## Verification boundaries

The release adds deterministic regression cases for the reported GUI paths and retains the surrounding strategy, order, storage and recovery tests. Exact test counts and results belong in the root [implementation and test report](../IMPLEMENTATION_TEST_REPORT.txt); this note does not treat planned checks as completed checks.

This host provides CPython 3.12.14 and headless Qt doubles. The renewed CPython 3.14 download and exact-requirements installation attempts were blocked by HTTP 403. Headless checks and the limited function-test fallback do not establish an actual pytest/coverage run, native Qt rendering, Python 3.14 compatibility, Ruff/Pyright completion, Windows executable packaging or live-broker verification. These remain separate target-platform gates unless the root report explicitly records their completion.

The [5.5.1 history release note](legacy/V5_5_1_HISTORY_SUMMARY.md) and [5.5.1 implementation report](legacy/V5_5_1_IMPLEMENTATION_TEST_REPORT.txt) are retained as historical records.
