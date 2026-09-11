# v4.0.0 ATR session memory and next-order risk editing

**Release:** v4.0.0

## Scope

Built on the final v3.9.0 source. This release adds validated prior-session ATR starting estimates and narrowly expands operator guard edits. It retains the five-stage engine, native order construction, order ownership, field-level quote checks, partial-fill settlement, two-observation final-SELL confirmation, reconciliation, watchdog, and audit-coalescing behavior.

## ATR starting values

Starting in v4.0.0, a validated ready ATR estimate is checkpointed in the existing SQLite `app_settings` table for the exact confirmed contract, currency, venue, trading/data profile, ATR period, and bar duration. At a verified open RTH session, that estimate can supply the starting ATR/ATR% while current-session bars warm up. It does not insert synthetic observations or make a quote fresh. The first ready current-session calculation replaces it. The GUI identifies the saved session and today's observed bar count.

The seed must be no more than seven calendar days old and must contain finite positive, internally consistent values observed inside its recorded RTH window. Weekends and short holidays can therefore reuse the most recent observed session; the application does not guess missing exchange sessions. First use, an expired/corrupt/mismatched checkpoint, or a changed ATR period/bar duration without a matching checkpoint retains normal warmup. v3.9.0 did not store these checkpoints, so the first v4.0.0 session needs enough observations once. A same-session application/watchdog restart can also reuse a valid checkpoint. Current market-data, opening-delay, RTH, gap, spread, two-observation SELL, and broker-reconciliation guards still apply. A saved estimate is not evidence that today's volatility is unchanged.

Only the volatility estimate is carried forward; current-session OHLC bars are collected separately using the unchanged calculation. No overnight gap or artificial tick is inserted. Current-session bars replace the starting estimate as soon as the normal calculation is ready. Enough current bars with an invalid/zero ATR do not silently keep an old estimate. Existing Stage-1 first-ready anchor behavior remains: readiness alone cannot trigger a BUY.

**Same-version saving-policy correction:** the latest valid, ready live-RTH estimate continues to update in memory throughout the session. Automatic SQLite checkpoint writes occur only during the **final five minutes of the broker-reported RTH window**, at most once per minute, plus one final flush on or after that window's recorded close. This is a saving window, not a five-minute ATR average or a change to the ATR calculation. Early closes and timezones use the existing broker session boundaries, not a fixed local closing time.

An **orderly application close** also saves the latest valid pending estimate, even earlier in the day. During ordinary intraday operation, a changed ATR configuration/contract or a temporarily missing/stale RTH status does not force a checkpoint write. The previous saved session is not routinely overwritten by the next morning's estimates. Continuous operation across a session boundary flushes the prior estimate before current-session bars reset. An incomplete warmup or a seed being reused cannot overwrite/re-date the original saved estimate.

Failures do not terminate logging or change order ownership. The final-close attempt may bypass the one-minute interval once; failed automatic writes retain the pending estimate and retry no faster than once per minute, including after close. Successful unchanged checkpoints are not written repeatedly overnight. The observation timestamp is preserved rather than replaced by the save time.

After an abrupt process/power failure before the closing window, that day's unsaved intraday estimate is lost; the last previously saved valid estimate remains the starting value. On first use, a crash before any eligible save or orderly close leaves normal warmup necessary. A blocked worker or failed storage cannot guarantee an app-close save. Existing v4.0.0 checkpoints remain compatible and retain the same identity, validity and seven-day age checks. Raw prices remain in memory; no SQLite schema change or additional worker is introduced.

## Risk and Timing edits before the next order

Reviewed guards can be edited during an active cycle. Edits are saved as explicit intent for the exact cycle/account/contract; an unrelated ticker draft cannot override an active or auto-repeat cycle. The manual input lock still prevents editing.

| Settings | Earliest application |
|---|---|
| Spread limit, delayed-data block, stale-data guard, selected-price age, bid/ask age, RTH-status age | While waiting in Stage 1 before a new BUY, or Stage 3 before a new final SELL. Edits entered during Stage 2 wait for BUY settlement; edits entered during Stage 4 wait for the next cycle. |
| ATR BUY-readiness block, hard-risk enable/limits, minimum trading price, previous-close gap, what-if check, recent-volatility enable/window/ceiling, session-timing enable, opening/closing BUY windows, BUY cancellation cutoff | Before the next BUY. A cycle already holding shares carries these edits to its next BUY/auto-repeat cycle. |
| Contract/account, entry budget/reinvestment after entry, parameters embedded in working native orders | Existing stage restrictions remain. A draft edit does not resize, modify, cancel, replace, or reprice a working order. |
| Optional close-before-RTH liquidation policy | Existing restrictions remain; Stage-4 changes that would change cancellation/liquidation of a working SELL stay locked. |

A working Stage-2 BUY retains its original partial-fill, safety-cancellation, and cutoff policy. A working Stage-4 SELL also retains its submitted terms. Quote-guard changes clear any first Stage-3 confirmation, so the next SELL requires fresh confirmation under the revised policy. BUY-only edits cannot retrospectively alter an already purchased position. Previously saved edits survive an application restart; reverting an edit clears the pending override. No change is made to the default values or trading formulas.

## GUI and exit choices

The top Profile card uses amber for LIVE mode; red remains available for errors. The redundant green minimum-profit text banner is removed from Live Strategy. The profit guard calculations, bounds, separate profit-guard graph, and controller checks remain enabled.

In the Stop strategy dialog, non-selling choices and their explanations precede the optional **Sell app-bought unsold position** action. **Exit app and resume/recover later** is bold. **Cancel** is the default/focused action, closes the dialog, and leaves the app running. Market selling is not the default and still requires its separate explicit confirmation. Exiting does not liquidate the position: existing native orders can remain at IBKR and locally monitored conditions are not evaluated while the app is closed. Reconciliation on the next start is unchanged.

## Upgrade and compatibility

Existing v3.9.0 databases remain compatible. No table, column, index, or cycle-state migration is introduced. Optional application-settings keys store ATR seeds and explicit next-order risk edits. Keep the existing database, account, contract, and client ID; retain normal backup and reconciliation procedures. The first run after upgrading has no retroactive v3.9.0 checkpoint and warms up once.

The source layout and Windows packaging scheme are unchanged. The source retains image assets, while the portable Windows root excludes the source Images directory and retains the runtime logo/icon within GUI. The root shortcut still targets GUI\IBKRTradingBot.exe. Ordinary and watchdog startup remain light-mode by default.

## Verification

See `IMPLEMENTATION_TEST_REPORT.txt` for measured gates and platform limitations. Tests use temporary SQLite databases, deterministic clocks, broker protocol doubles, and headless Qt doubles. They do not submit live orders. A native Windows build and two-session paper-trading smoke test remain required before unattended live use.

The corrected v4.0.0 source tree passed **1,310/1,310** pytest cases across **130 test modules**, with `ResourceWarning` promoted to an error and no skipped or expected-failure cases. The complete suite ran in one fresh Python process; per-module process-isolated reruns were not repeated for this correction. The measured combined statement/branch coverage was **78.8%** (82.3% statements; 68.1% branches), and **1,038/1,038** executable application callables were entered. The release killed **17/17** safety mutants and passed **58/58** deterministic simulation contracts across 54 CSV paths. The four v4.0.0 modules contain **99 focused regression cases**, including **26 new ATR checkpoint-timing cases**. Ruff and Pyright could not run because their packages were unavailable; native Windows/PyInstaller, real Qt, and live IBKR validation remain unperformed.
