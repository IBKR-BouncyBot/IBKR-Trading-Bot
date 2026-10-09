# v5.7.0 Partial-BUY completion

Version 5.7.0 keeps an already partially filled marketable BUY working on its original broker order. It removes the three-second partial-fill timeout and automatic post-fill cancellation caused by entry or market-data guard changes. This applies to direct `MKT` BUYs and native `TRAIL` BUYs after they trigger and start executing.

## Supplied incident

The audit records a BUY for 15 shares, a 10-share execution at 655.72, and cancellation of the five-share remainder after the stale-data guard found an old bid-price update. The BUY-fill market capture shows live data and an open regular session, with a bid update age of approximately 9.44 seconds against the configured seven-second limit. Ask and Last updates were approximately 0.19 seconds old. These observations match the old cancellation rule; they do not establish a broken connection or prove that the remaining shares would have filled.

The recorded order was a native trailing BUY that had triggered and partially executed. The new rule therefore covers that case as well as a directly submitted market BUY.

## Changed behavior

- A positive nonterminal BUY fill leaves the original order working and the cycle in Stage 2. There is no elapsed partial-fill deadline and no remainder cancellation solely because the entry/data guards change.
- Further executions update the same order's acquired quantity, weighted average price and commissions. Existing execution-ID deduplication and persistence remain in use.
- Stage 3 starts only when the original BUY is terminal. If IBKR confirms a terminal partial cancellation or rejection, management uses only the acquired shares. The bot does not submit a replacement BUY to top up the quantity.
- The same policy applies after reconnect or restart when the working order has been reconciled. An old first-fill timestamp is not a cancellation deadline.

The separate configured pre-close BUY cancellation and explicit operator Stop/close workflows still apply. Existing identity/quantity conflict checks, cancellation confirmation and late-fill accounting are unchanged. A cancellation already accepted by IBKR cannot be reversed by installing this release.

Native order restrictions, including `outsideRth=False`, remain unchanged. Keeping the order working permits the broker to finish it; it does not guarantee execution time, price or full completion. Protective SELL placement still waits until the original BUY is terminal, so a prolonged working partial remains in Stage 2 before protection is placed.

## Scope and compatibility

Only the controller's partial-BUY cancellation policy changes executable trading behavior. The pure strategy layer has a matching docstring correction, and GUI edits only update version strings. New-order entry checks, Stage-3 SELL quote checks, SELL handling, recovery, order sizing, strategy thresholds, ATR/RTH rules, GUI layout, database schema, dependencies and backup cadence retain their previous behavior. Existing 5.6.2 databases remain compatible; no migration or new setting is introduced.

The [5.6.2 completed-order recovery](legacy/V5_6_2_COMPLETED_ORDER_RECOVERY.md) and [5.6.1 outage recovery](legacy/V5_6_1_OUTAGE_RECOVERY.md) remain part of the release.

## Upgrade

1. Preserve each instance's current source/executable and existing database, configuration, captures and backups. Use the normal **Exit app and resume/recover later** path.
2. Use `IBKR_Trading_Bot_5.7.0_Source.zip`, or apply `IBKR_Trading_Bot_5.7.0_Release_files.zip` to its exact 5.6.2 baseline and follow the archive's replacement/deletion instructions. Do not replace a live database with an audit copy.
3. Keep standard GIL-enabled CPython 3.14.x and the unchanged project requirements. Run `run_all_tests.bat` on the supported Windows environment and rebuild a packaged executable. Copying source files beside an old executable does not change that executable.
4. Restart, connect and resume the saved cycle through the normal recovery path. Compare the exact broker order and acquired quantity. A remainder already cancelled under the earlier version stays cancelled; this release does not purchase the missing shares automatically.

## Verification boundaries

[`test_v570_amd_partial_buy_replay.py`](../tests/test_v570_amd_partial_buy_replay.py) uses the [sanitized fixture](../tests/fixtures/v570_amd_partial_buy.json) from the supplied audit and BUY-fill market-data capture through deterministic broker boundaries and disposable SQLite state. Observed quantities, prices, quote ages and terminal cancellation are kept distinct from controlled later fills used to prove that the new policy permits completion. Tests also retain explicit cancellation paths and the original acquired-quantity/commission settlement rules.

The root [implementation and test report](../IMPLEMENTATION_TEST_REPORT.txt) records executed checks, review results and tool availability. A full-fill response injected in a deterministic test is not evidence that the historical remaining shares would have filled at IBKR. The source audit and capture archives are not modified or distributed with the public source.

The [manual test plan](TEST_PLAN.md) includes an outstanding Windows/Python 3.14 paper-account check for working BUY remainders and cancellation races. Host tests do not establish native Windows/Qt rendering, actual pytest/coverage, Ruff/Pyright completion, a Windows executable build or broker integration. No test for this release submits an order to the user's broker.
