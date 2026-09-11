# v3.4.0 reliability and recovery corrections

**Release:** v3.4.0

**Release date:** 2026-07-27

**Type:** broker-fill safety, watchdog/process recovery, persistence consistency, Windows lock handling, capture shutdown, and maintenance release

## Summary

v3.4.0 applies and reviews a focused set of reliability fixes on top of the
corrected v3.3.0 source. It does not change the five-stage strategy formulas,
configured thresholds, normal order types, position sizing, RTH definitions, or
SQLite schema. The release concentrates on preventing ambiguous broker or local
state from being interpreted as a completed cycle and on making unattended
shutdown/restart paths bounded and deterministic.

## Watchdog process replacement on Windows paths with spaces

Windows watchdog replacement now launches the replacement process with an
argument list through `subprocess.Popen`. Python applies the native Windows
command-line quoting rules, so a source tree, Python executable, or packaged
application located under a directory containing spaces remains launchable.
POSIX hosts retain `os.execv` and its atomic process-image replacement behavior.

The one-instance lock is still released before replacement. The existing
one-time restart token, exact-cycle signature checks, reconciliation gates, and
restart-rate limits are unchanged.

## Protective SELL partial-fill safety

Protective SELL processing now requires proof that the complete app-owned BUY
quantity has been sold before the cycle can be completed:

- A live nonterminal partial fill is recorded and persisted while the original
  protective order remains under supervision.
- A terminal partial order, such as `Cancelled` after 40 of 100 shares, enters
  `ERROR`, writes a `PROTECTIVE_SELL_PARTIAL_TERMINAL` decision event, and leaves
  the remaining quantity for explicit broker/local reconciliation.
- A broker status with zero remainder but a filled quantity different from the
  app-owned quantity enters `ERROR` with a
  `PROTECTIVE_SELL_QUANTITY_MISMATCH` decision event.
- Reconnect recovery uses the same exact-quantity gate as uninterrupted live
  polling.
- Execution-led recovery for ordinary and protective SELLs completes a cycle
  only when the aggregate recovered quantity exactly equals `buy_filled_qty`.
  Both under-counts and over-counts persist the observed facts and fail closed.
- After a protective partial has been observed, a temporarily unavailable order
  poll blocks price-triggered exit logic rather than allowing a second exit
  order to be created from incomplete broker facts.

These checks preserve broker evidence and prevent false cycle completion,
incorrect P/L, stranded shares, and accidental duplicate exits.

## Lockfile correctness on Windows

The Windows process-liveness probe now loads `kernel32` with
`use_last_error=True` and declares the relevant `OpenProcess`,
`GetExitCodeProcess`, and `CloseHandle` signatures. An access-denied elevated
owner can therefore be distinguished from a dead process instead of causing the
portable-folder lock to be removed incorrectly.

If lock acquisition creates the lock file but writing the owner PID fails, the
file descriptor is closed, the partial file is removed, and the in-process lock
registry is cleaned before the exception is returned.

## Market-capture writer shutdown

The asynchronous market-capture writer now survives individual file-write
failures so later queued captures can still be processed. Shutdown no longer
uses the unbounded `Queue.join()` operation. It restarts a missing or dead writer
when work is queued, polls unfinished work against a monotonic hard deadline,
then performs only the remaining bounded thread join.

A disk-full or failed capture therefore cannot indefinitely block normal exit or
a watchdog-requested process replacement.

## Persistence and recovery consistency

- Active-cycle selection now orders same-second rows by `updated_at DESC`, then
  `cycle_number DESC`, then `id DESC`, making startup/watchdog selection
  deterministic.
- The GUI-thread emergency resume-checkpoint fallback accepts a cycle only after
  two consecutive serializations match. If the worker keeps mutating the object
  throughout the bounded read window, checkpoint creation fails instead of
  writing a potentially torn cycle.
- Tokenless startup removes only expired or malformed watchdog request files.
  A fresh, valid request remains available to the token-holding replacement
  process.
- Execution fallback deduplication now includes the execution timestamp, so two
  same-side, same-size, same-price partial prints are not merged when IBKR omits
  an execution ID.

## Maintenance corrections

- Removed the dead duplicate headless signal-instance definition.
- Rounded the initial `drop_trigger_price` to the same four-decimal precision
  used by subsequent strategy recalculation.
- Normalized built-in TWS and IB Gateway Windows paths to single separators.
- Retained the existing non-negative cumulative commission placeholder contract;
  signed per-execution commissions remain authoritative for P/L.

## Compatibility

v3.4.0 adds no table, column, index, migration, or persisted setting. Existing
v3.3.0 databases, settings, active cycles, orders, executions, audit events,
backups, exports, market captures, and watchdog files continue through the same
startup and reconciliation paths.

The release changes only ambiguous/error handling and maintenance behavior.
Strategy percentages, ATR calculations, price-source priority, order
construction, quantity sizing, commission accounting, RTH rules, and account
scope are otherwise unchanged.

## Verification scope

Focused deterministic regressions cover:

- Windows replacement argument handling with paths containing spaces and POSIX
  `execv` preservation;
- live, terminal, recovered, under-counted, over-counted, and temporarily
  unavailable protective SELL states;
- exact-quantity execution-led SELL recovery;
- stable and continuously changing emergency checkpoint serialization;
- fresh, expired, and malformed watchdog request cleanup;
- last-error-aware Windows process probing and lock-write cleanup;
- capture-write failure survival, dead-writer restart, and hard shutdown bounds;
- deterministic active-cycle ties;
- timestamp-sensitive execution deduplication;
- initial trigger rounding, built-in platform paths, and headless signal cleanup.

The complete verification results for the released archive are recorded in the
root `IMPLEMENTATION_TEST_REPORT.txt`.

## Windows validation boundary

The Windows-specific branches are covered with deterministic platform doubles.
A native Windows packaging and live IB Gateway smoke test remains necessary for
final deployment validation of the generated PyInstaller executable, shortcut,
process replacement, and broker reconnection in the intended installation path.
