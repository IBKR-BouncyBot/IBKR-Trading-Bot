# v5.1.0 targeted trading correctness fixes

Version 5.1.0 addresses the eleven confirmed defect groups from the 25 September 2026 read-only review of the final 5.0.0 source. It retains the five-stage strategy, broker-native orders, configured thresholds, saved ATR seed policy and standard GIL-enabled CPython 3.14.x requirement.

## Changes and regression coverage

| Review ID | Correction | Focused regression module |
|---|---|---|
| F01 | Keep established connection identity separate from editable drafts. A changed endpoint/client/profile requires a matching re-established session before trading. The status profile continues to identify the established session until reconnection. | `test_v510_gui_commands.py` |
| F02 | Reject contradictory execution ownership during recovery. A matching numeric order ID does not override a conflicting permanent ID, account or contract. Ambiguous recovery remains blocked. | `test_v510_recovery_commissions.py` |
| F03 | A timeout checkpoint preserves the committed cycle row instead of overwriting it from an older GUI-thread snapshot. A waiting-entry cycle with an exact-known working order requires reconciliation. | `test_v510_checkpoint.py` |
| F04 | Keep supervising a cancelling BUY before sizing a manual market close; continue an explicitly requested close after confirmed partial SELL cancellation using reconciled remaining quantity; check known replacement eligibility before cancelling an existing exit. | `test_v510_manual_close.py` |
| F05 | Bind Mark manually handled to the reviewed cycle and its broker-relevant state, checking after the dialog and again in the worker. A changed price alone does not invalidate acknowledgement. | `test_v510_gui_commands.py` |
| F06 | Repeat enabled time-sensitive BUY checks after backup/intent persistence and before transmission. An untransmitted rejected intent is recorded and rolled back without treating it as an uncertain broker submission. | `test_v510_data_timing.py` |
| F07 | Preserve an unconsumed selected-price/quote update when a later size callback arrives before strategy evaluation. Size-only events do not refresh price age or count as another SELL confirmation. | `test_v510_data_timing.py` |
| F08 | Preserve a stable UTC completion date for realized cycle P/L and risk ordering. Late commissions can change the amount without moving it into another day. | `test_v510_strategy_risk.py` |
| F09 | Invalidate ATR readiness when period/bar duration changes, and verify calculation identity before allowing an entry guarded by ATR warmup. | `test_v510_strategy_risk.py` |
| F10 | Age production quote observations using measured monotonic elapsed time, preserving subsecond accuracy and avoiding wall-clock adjustment errors. | `test_v510_data_timing.py` |
| F11 | Retain signed authoritative commissions/rebates and apply currency validation consistently when projecting execution totals into cycle totals. | `test_v510_recovery_commissions.py` |

The top-row lock button now fills the same vertical layout space as the adjacent status boxes. Its width, text size and locking behavior are unchanged. `test_v510_lock_button_geometry.py` checks actual Qt geometry in isolated processes at 100%, 150% and 200% scaling, both themes and both lock states; it explicitly skips when native PySide6 is unavailable.

Tests cover the reported failures and successful adjacent paths. Broker doubles use temporary SQLite databases; native geometry tests do not connect to a broker. See the root [verification report](V5_1_0_IMPLEMENTATION_TEST_REPORT.txt) for measured results and unavailable gates.

## Operator-visible behavior

When connected and idle, connection settings can still be edited. If they no longer match the established session, Start is blocked and Connect requests a deliberate reconnect. The existing automatic reconnect path can also establish the current draft after a disconnect. Merely selecting Paper does not relabel the current Live session as Paper. Active-cycle identity locks remain in force.

Manual market close remains an explicit close of app-owned unsold shares. A pending/failed BUY cancellation cannot authorize an undersized SELL. A confirmed cancellation of an existing partially filled exit can continue the already-requested close for the reconciled remainder. Unknown outcomes, contradictory quantities and unexpected terminal partial replacements still require manual review. If RTH is already closed, the application does not cancel a working exit for a replacement it already knows it cannot submit. Conditions are rechecked after cancellation as well; cancellation and replacement are separate broker operations.

Changing ATR period or interval can return a waiting entry to warmup. Unchanged calculations and valid saved ATR estimates retain their existing behavior. A size-only event neither creates a sample nor discards a preceding unconsumed price sample. The original price receipt time determines its observation bucket.

## Database compatibility

This release adds **one nullable `cycles.completed_at` TEXT column** through the existing additive migration path. Storage records the first persisted transition to `CYCLE_COMPLETE` and preserves that timestamp on later updates. Daily completed-cycle P/L/count queries and consecutive-loss ordering use this stable completion evidence. The accounting day remains UTC; this is not an exchange-local day or real-time account P/L calculation. A fill completed while the app was offline and first recognized on a later day belongs to that application-completion day; this field does not reconstruct broker execution time.

For an existing completed row without this field, migration retains its current `updated_at` as the one-time legacy completion fallback. If an older release already moved that timestamp, 5.1.0 cannot reconstruct the original completion date reliably and does not guess from the first partial SELL time. No historical fills, order references, accounts or quantities are rewritten. The old `sell_filled_at` field can describe the first partial sale and is not substituted for final completion.

Supported older databases continue through the additive opening path. Use an application-created backup or a cleanly closed database for transfer; keep the original installation and backup until the new build has been checked. There is no reset of the saved ATR checkpoints or current order identities.

## Upgrade and qualification

1. Retain a consistent database backup and the working installation. Close the instance before replacing its application files; do not copy another instance's database/client settings over it.
2. Use the full 5.1.0 source or apply the exact-baseline patch only to the final reviewed 5.0.0 source. The release's changed-files list identifies the affected files.
3. Source launch/test/build continues to require standard GIL-enabled CPython 3.14.x and the existing dependency ranges. No dependency upgrade or Python-branch change is part of 5.1.0.
4. Run `run_all_tests.bat` on Windows. It is the full pytest, coverage, callable-entry, mutation, simulation, Ruff and Pyright gate. `scripts/build_windows.ps1 -RunTests` runs the narrower build checks and produces the Windows executable; source files alone do not update an existing executable.
5. Check the changed workflows with the target Windows/Qt build and an isolated paper-broker setup before relying on that build unattended. These checks must preserve unique instance folders/client IDs and actual broker ownership evidence.

The development host could not install Python 3.14 or the declared dependencies because downloads returned HTTP 403. Offline CPython 3.12 results and the limited function-test runner are not claims that native Windows, real pytest/coverage, Ruff, Pyright, PyInstaller or IBKR integration passed. The release verification report records exactly what ran.

## Scope retained

The release does not implement the review's excluded shutdown-queue observation or its Linux-only lock reproduction. There is no worker/database architecture consolidation, strategy-threshold adjustment or unrelated GUI redesign. The [archived 5.0.0 note](V5_0_0_TRADING_SAFETY_AND_PYTHON314.md) records the preceding Python migration and other historical changes.
