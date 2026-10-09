# v5.6.1 Gateway outage recovery

Version 5.6.1 addresses two confirmed recovery failures in the supplied PURR and ASML audit bundles. It prevents incomplete broker reads from creating permanent manual-review holds and adds a narrowly checked route through the existing **Reconcile and resume** action for those two legacy hold reasons.

## Findings from the supplied bundles

| Instance | Recorded failure | Later evidence |
|---|---|---|
| PURR | Recovery attempted to qualify the stored holding while the connection was unstable and saved `Cannot qualify the stored position contract: Not connected to TWS.` as a permanent manual-review reason. | A later broker refresh reported 702 shares, matching the bot's recorded holding, but Reconciliation still returned the original manual-review reason. |
| ASML | Recovery saved a hold saying the pinned cycle account was not confirmed in the managed accounts at the same time as a recorded API disconnect. A temporarily unavailable account list is consistent with that sequence; the bundle does not directly record the list returned by the failing call. | Later diagnostics listed the pinned account again. The waiting-entry cycle had no recorded order references, orders or executions, but Reconciliation still retained the hold. |

Both bundles show a brief reconnect followed by another disconnect: recovery saved the false hold at about **11:51:23 UTC / 13:51:23 Europe/Amsterdam on 5 October 2026**. The brief connection window let recovery start, but the connection was no longer stable when it read the necessary broker facts.

In both cases, the previous recovery path checked the persisted manual-review flag before attempting any supported restoration. Refreshing broker data therefore could not remove the hold. The supplied bundles do not identify the installed source version, establish when the defect was introduced, or prove that every other bot instance has the same failure.

## Changed recovery behavior

Recovery requires completed authoritative broker reads and checks connectivity around them. Open-order, execution and position request errors, timeouts or disconnects cannot become successful empty results or cached substitutes. A temporarily unavailable managed-account snapshot also defers recovery. The saved stage is retained, trading stays paused, and the existing recovery retry path tries again when broker connectivity is ready. This adds no background worker and does not change the ten-second local reconnect interval or the existing two-second minimum interval between pending recovery attempts.

The detailed status reports **Broker recovery is waiting for complete broker data; trading remains paused** with the read-failure reason. During an active cycle, the Connection box shows amber **Reconnecting** for local connection loss, amber **Waiting for IBKR** for upstream loss, and amber **Reconciling** while broker reconciliation is pending. Affected workflow cards show amber **Waiting**, and Trading shows **Paused: reconnecting** or **Paused: reconciling**. Actual manual-review and trading-risk faults remain red. Successful reconciliation still requires new post-recovery market-data evidence before ordinary strategy evaluation can continue. Native orders already accepted by IBKR retain their existing behavior during the outage; this correction does not cancel them merely because the connection was interrupted.

An interrupted read-only identity check before any order was transmitted also waits. Proven-unsent ordinary BUY/SELL plans return to their waiting stage; queued operator-close and auto-repeat requests can continue after recovery. An operator-close request retains Stop-after-current-cycle so a fill during the outage cannot start another cycle. These deferred request references are held in memory and cleared by operator Disconnect; they are not new restart instructions. An order that may already have been transmitted still requires exact-order reconciliation. Missing required protection remains a genuine exposure problem and retains manual review.

An explicit operator Disconnect shows **Disconnected / Paused: disconnected**, with Connect available. It does not imply that automatic reconnection is still running.

## Previously persisted outage holds

The existing **Reconcile and resume** button is the action referred to as “Reconcile and continue” in the incident report. It may restore only the two exact supported legacy error reasons above, after all required evidence agrees:

- The same cycle is the only unresolved cycle, and its audit proves the hold and the preceding Stage 1 or Stage 3 waiting state. Current broker holdings alone cannot establish that prior stage.
- Stage 1 has no recorded order reference, order or execution evidence. Stage 3 has a fully filled BUY whose local order and execution identities and quantities agree, with no SELL/protective-order or cancellation/market-close transition.
- Fresh broker reads confirm the pinned account, exact qualified contract, order and execution state, and sufficient exact-account/contract position. A working app-owned order, new/conflicting fill or insufficient position cannot authorize restoration.
- The persisted cycle has not changed while the broker evidence was acquired.

A successful restoration is recorded as `OUTAGE_HOLD_RECONCILED`; normal recovery and all trading guards then apply. Reconnect or Start alone does not automatically release an existing hold. If a read is temporarily incomplete after the explicit request, that request is retained in memory for the same cycle across the existing recovery retries. It is cleared when the attempt finishes or the operator disconnects; it does not survive an application restart or authorize another cycle. Incomplete evidence leaves trading paused. Unknown submissions, other manual-review reasons, ambiguous orders, pending exits/protection, absent or conflicting audit history, wrong account/contract identity and genuine position shortfalls remain blocked.

## Scope

The correction covers recovery reads and decisions, proven-unsent identity-read deferral, the read-only storage lookup needed to prove a legacy waiting stage, and GUI presentation of temporary outage waits, plus tests, release metadata and documentation. There is no database schema migration or automatic rewrite of the supplied databases. Order sizing, strategy thresholds, RTH/ATR/quote guards, backup policy and normal native-order behavior remain under their existing contracts. Recovery can still apply broker executions and persist legitimate stage transitions through the established paths.

The retained [5.6.0 GUI corrections](V5_6_0_TARGETED_GUI_FIXES.md) and [verification report](V5_6_0_IMPLEMENTATION_TEST_REPORT.txt) remain available as historical records.

## Upgrade and recovery

1. Exit each instance through its normal **Exit app and resume/recover later** path. Keep its existing database, configuration, images, captures and backups, and retain the prior working source/executable. Do not replace or edit the live database with an audit-export copy.
2. Use `IBKR_Trading_Bot_5.6.1_Source.zip`, or apply `IBKR_Trading_Bot_5.6.1_Release_files.zip` to the exact 5.6.0 baseline named in that archive. Follow the archive's file-replacement/deletion instructions.
3. Keep standard GIL-enabled CPython 3.14.x and the project requirements. Run `run_all_tests.bat` in the supported Windows environment. A packaged executable must be rebuilt from the updated source; copying Python files beside an older executable does not update that executable.
4. Restart the instance and connect to the saved account/session. Inspect its actual orders, fills and position directly in TWS/Gateway.
5. Open Reconciliation, press **Refresh from IBKR/TWS**, wait for **Current**, and compare the local and broker facts. Then select **Reconcile and resume** for the existing outage hold. A successfully restored stage remains subject to normal startup, quote, RTH, ATR and order guards.
6. If manual review remains, export a new audit bundle with the displayed reason. Do not edit SQLite or use **Mark manually handled** simply to bypass the hold. Different errors or ambiguous orders require their own evidence and resolution.

## Verification boundaries

Focused offline regressions cover failed/interrupted recovery reads, delayed managed accounts, successful retries without stage loss, the two supported explicit legacy restorations, and retained fail-closed behavior for uncertain submissions, identity conflicts, missing evidence, working/exit/protective orders and insufficient positions. The [implementation and test report](V5_6_1_IMPLEMENTATION_TEST_REPORT.txt) records the exact checks executed and their results; planned tests are not reported here as completed gates.

The development host provides CPython 3.12 and headless test doubles. These do not establish standard CPython 3.14 runtime validation, an actual pytest/coverage run, Ruff/Pyright completion, native Windows/Qt rendering, Windows executable packaging or live IBKR behavior. Use the report for current tool availability and remaining gates. No verification in this work connects to the user's broker or submits a live order.
