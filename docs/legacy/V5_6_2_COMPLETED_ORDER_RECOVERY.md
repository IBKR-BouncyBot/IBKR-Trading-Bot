# v5.6.2 Completed-order execution recovery

Version 5.6.2 fixes an execution-reconciliation defect reported on 5.6.1. A native SELL completed at IBKR while the application was disconnected, but the bot later remained in Stage 4 because an order object's summary counters hid the matching execution. The fix reconciles that execution through the existing local ledger and cycle-completion path.

## Confirmed incident

The supplied audit bundle records a native trailing SELL submitted before the overnight Gateway outage and a complete broker execution during the following regular session. The user confirmed that the running application was 5.6.1. The local cycle still showed Stage 4 and no recorded SELL quantity at export time, even though the archived broker data included the complete matching execution.

The cached order object initially retained its older state. A later completed-order object had status `Filled` and zero summary filled quantity while its attached execution reported the actual shares and price. The previous recovery path did not consult execution recovery when an order object existed, so that execution never entered the local ledger. Commission reports then remained pending, and recovery could incorrectly report the cycle fully reconciled.

This establishes a local accounting/recovery failure, not a missing broker SELL. The bundle does not establish why the broker executed several minutes after the saved session opening. That timing requires IBKR's order/trigger history. This release does not change the native order's trigger, RTH setting or market execution behavior.

## Changed behavior

- Matching execution evidence is considered even when a cached or completed-order object exists. A stale or zero order-summary fill counter cannot by itself suppress an exact app-owned execution.
- Recovery applies the execution to the existing ledger and cycle state. A fully evidenced final SELL can complete Stage 5 and update the recorded result; a partial SELL without a confirmed working exit remains incomplete and requires review. Existing explicit close workflows retain their cancellation/remainder rules.
- Strict recovery snapshots reject repeated execution IDs with conflicting economic or ownership evidence instead of silently keeping one row. Recovery waits for a later consistent snapshot, which can complete reconciliation without another order. Legitimate late fee updates, including an authoritative zero, remain accepted; normal execution polling retains its existing behavior.
- Repeated execution IDs do not add the same shares twice. Recovered SELL totals include previously recorded executions that fall outside the current broker response, and cumulative placeholders are reduced when actual executions arrive.
- Valid broker execution timestamps populate missing fill timestamps instead of using the later reconnect time. A previously validated pending commission is applied only when its matching execution is recorded; authoritative zero or corrected fees retain the existing currency and deduplication rules.
- An unresolved fill discrepancy does not count as successful reconciliation. The application retains the relevant waiting/review state until adequate broker evidence is available.

Ownership still requires the exact recorded order identity: the complete `OrderRef`, or a proven recorded permanent ID when a legacy execution omits its reference. Available account, contract and side evidence must not conflict. A numeric order ID alone cannot establish ownership.

The new completed-order counter normalization is stricter: its attached executions must carry matching order reference, permanent ID, account, exact contract and side, with unique execution IDs and a complete matching quantity. The separate recent-execution fallback retains the existing legacy-compatible ownership rules above. A `Filled` label, an empty open-order list or a zero account position alone cannot establish execution quantity or price, create a fill, or authorize another SELL. Unknown transmissions, contradictory identity, quantities beyond the app-owned holding and genuinely uncertain orders retain their safety checks.

## Scope and compatibility

This is a targeted correction to broker execution interpretation and controller reconciliation, with tests, current documentation and release metadata. There is no database schema migration. Existing 5.6.1 databases are retained and missing fills are added only through normal validated broker recovery; supplied audit databases are not substituted for live state.

The five-stage strategy, order sizing, RTH/ATR and quote guards, normal stop/trailing behavior, backup policy and existing outage retry cadence remain under their existing contracts. The [5.6.1 outage protections](V5_6_1_OUTAGE_RECOVERY.md) remain in place. The correction does not clear arbitrary manual-review holds or sell a position merely because the GUI still shows Stage 4.

## Upgrade and recovery

1. Exit each instance through its normal **Exit app and resume/recover later** path. Preserve its existing database, configuration, captures, backups and prior executable/source. Do not replace the live database with an audit-export copy.
2. Use `IBKR_Trading_Bot_5.6.2_Source.zip`, or apply `IBKR_Trading_Bot_5.6.2_Release_files.zip` to the exact 5.6.1 baseline named in the archive. Follow its file-replacement/deletion instructions.
3. Keep standard GIL-enabled CPython 3.14.x and the project requirements. Run `run_all_tests.bat` in the supported Windows environment. Rebuild a packaged executable from the new source; copying Python files beside an older executable does not update it.
4. Restart, connect to the saved account/session and compare the exact order, execution and remaining position directly in TWS/Gateway. Use the normal **Start strategy** path to resume a restored cycle; if Reconciliation reports a mismatch, obtain a current **Refresh from IBKR/TWS** probe and use **Reconcile and resume** when available.
5. Confirm that the recovered execution appears in the cycle audit and that the local SELL quantity/result agrees with the broker evidence. Repeated refresh or recovery must not add the same fill twice. Auto-repeat remains subject to the existing saved settings and startup/trading guards.
6. If the execution is no longer available to the API, evidence conflicts or a hold remains, preserve a fresh audit bundle and the exact IBKR execution/order history. Do not submit a second SELL solely because the old local stage still shows a holding, edit SQLite, or mark a cycle manually handled merely to hide the discrepancy.

## Verification boundaries

Focused offline regressions exercise stale and completed-order counters, matching and mismatched execution identity, partial fills, duplicate observations, commission timing and retained outage protections. The multi-audit replay matrix passed 296 named tests using 25 sanitized observed order shapes from six supplied, overlapping archives: 15 complete orders (six BUY, nine SELL), six working orders and four cancelled orders. These are not six independent databases. The matrix uses the original response condition and 24 controlled variants where applicable; it is not a complete cross-product or proof of every possible broker condition.

These tests run the actual strict adapter Fill normalization, controller recovery and SQLite paths with deterministic broker-request responses. The matrix exposed 12 failures in the draft correction: duplicate filtering discarded conflicting execution evidence before recovery could see it. The released strict recovery path now rejects those inconsistent snapshots and permits a later consistent retry. See the [matrix scope](../AUTOMATED_TEST_COVERAGE.md#v562-multi-audit-recovery-matrix) for the distinction between archived evidence and controlled variants. The root [implementation and test report](V5_6_2_IMPLEMENTATION_TEST_REPORT.txt) records the exact tests run, supplied-evidence replay, independent review and results. Coverage statements here describe scope and do not substitute for those executed results.

The [Windows/Python 3.14 paper-account acceptance procedure](../TEST_PLAN.md#windowspython-314-paper-account-acceptance-gate) specifies a real native SELL filled while the app is disconnected, reconnect/restart recovery, ledger/fee/timestamp verification and an explicit no-extra-SELL check, plus connected and partial-fill controls. This remains an outstanding integration gate; it was not performed as part of the offline host verification.

The target runtime remains standard GIL-enabled CPython 3.14.x on Windows. Headless test doubles and a different host Python cannot establish native Windows/Qt rendering, supported-runtime validation, actual pytest/coverage, Ruff/Pyright completion, a Windows executable build or live broker behavior. Consult the current report for tool availability and remaining gates. No verification for this release connects to the user's broker or submits a live order.
