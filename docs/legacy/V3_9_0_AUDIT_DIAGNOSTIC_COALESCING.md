# v3.9.0 audit diagnostic coalescing and live guard status

**Release:** v3.9.0
**Previous release:** v3.8.0
**Release type:** audit/diagnostic reliability and operator-visibility update

## Purpose

Long-running cycles can evaluate the same valid safety condition many times per second. In v3.8.0, several of those routine observations were written as independent SQLite audit events. The most visible example was the Stage-3 final-SELL quote guard: an independently stale ask could generate a new warning whenever its displayed age changed, even though the underlying reason had not changed. A 52-minute NBIS audit contained 705 `Final SELL trigger not armed` warnings, with many rows differing only by a fraction of a second.

v3.9.0 retains every trading, market-data, order, recovery, and risk decision from v3.8.0 while changing how persistent operational conditions are represented. The controller now keeps the current condition in memory for live operator display and writes a bounded sequence of structured audit events instead of one row per cadence.

## Stable diagnostic conditions

Each coalesced condition uses a stable key and stable reason code. Variable details such as quote age, price, spread, retry count, or wall-clock timestamp are retained as structured context, but they no longer become part of the suppression key.

The coalescer records:

- first and latest observation timestamps;
- total observation count;
- per-reason counts;
- the latest detailed message and context;
- observed maximum numeric metrics;
- immediate-event count;
- periodic-summary count; and
- the number of cadence observations suppressed from SQLite.

When a condition is operationally relevant, audit behavior is:

1. one condition-entry event;
2. one persistence summary after the configured delay;
3. further summaries at a bounded interval; and
4. one recovery event when the condition clears.

The state itself remains evaluated on every normal controller cadence. Suppressing duplicate audit rows does not suppress, delay, weaken, or bypass a guard.

## Stage-3 final-SELL quote evidence

Stage 3 still requires the complete v3.7.0 market-data safety boundary:

- independently fresh bid and ask fields;
- a complete, positive, non-crossed quote;
- spread within the configured Maximum spread percentage;
- a fresh executable SELL-side bid at or above the recalculated profit trigger;
- two distinct qualifying quote observations; and
- revalidation before durable intent and again before the broker call.

v3.9.0 assigns stable reason codes to routine failures, including:

- `FIELD_TRACKING_UNAVAILABLE`
- `NO_DISTINCT_UPDATE`
- `UPSTREAM_UNCONFIRMED`
- `EVENT_IDENTITY_UNAVAILABLE`
- `INCOMPLETE_QUOTE`
- `CROSSED_QUOTE`
- `QUOTE_TIME_UNAVAILABLE`
- `NO_PRICE_FIELD_UPDATE`
- `STALE_QUOTE`
- `FIELD_TIMESTAMPS_UNAVAILABLE`
- `STALE_BID_ASK`
- `STALE_BID`
- `STALE_ASK`
- `SPREAD_TOO_WIDE`
- `TRIGGER_UNAVAILABLE`
- `TRIGGER_NOT_REACHED`

A fresh valid quote whose bid remains below the trigger is ordinary Stage-3 waiting and does not create repeated warning events. Cached reads and non-price callbacks are shown in the GUI but do not create audit rows by themselves.

An invalid quote is immediately elevated when:

- the executable/reference price is within 0.5% of or above the current trigger;
- a first qualifying SELL confirmation had already been accepted and is then invalidated;
- field tracking, event identity, upstream connectivity, or the trigger itself is unavailable; or
- the final pre-submit revalidation fails after the SELL was otherwise confirmed.

The Price Data Monitor now displays the current Stage-3 evidence state continuously, including stable reason code, detail, executable bid, trigger, percentage distance, confirmation state, observation count, and suppressed-row count. This is the primary location for routine current-state detail; the audit log remains a transition and persistence record.

## Reconnect diagnostics

A connection outage now creates one global reconnect condition:

- the outage is logged immediately as WARN;
- repeated ten-second reconnect attempts continue unchanged but do not each write a WARN row;
- one INFO persistence summary is eligible after one minute;
- later WARN summaries are eligible every five minutes; and
- restoration produces one INFO recovery event.

The raw summary evidence retains attempt counts and current connection context. Manual Disconnect clears the condition without a recovery event because the outage is no longer being supervised automatically.

## Native trailing-order waits

A normal unchanged native BUY or SELL trailing order is an expected broker state. v3.9.0 therefore records an INFO persistence summary no more frequently than every 15 minutes rather than one warning per minute.

More relevant diagnostics remain immediate:

- the selected application price crossing the displayed initial stop produces an INFO condition event;
- raw Last crossing the displayed stop produces a WARN condition event; and
- anomalies receive bounded periodic WARN summaries while they persist.

Order submission, status changes, fills, partial fills, cancellations, terminal states, rejections, missing orders, and quantity inconsistencies retain their existing immediate audit behavior.

## BUY preflight and session timing

Repeated BUY preflight blockers are coalesced per cycle while retaining stable blocker reason codes. Expected waiting states such as ATR warmup, RTH closed, and session timing begin as INFO, receive their first INFO persistence summary after 15 minutes, and then summarize at most every 15 minutes. Hard risk/data/broker blockers begin as WARN, receive their first WARN persistence summary after one minute, and then summarize at most every five minutes. A blocker that escalates from an expected wait to a hard failure adopts the stricter WARN cadence. A successful transition to order submission records recovery once.

Close-before-RTH boundary, profitability, cancellation, and settlement waits use the same condition model. Missing session boundaries remain immediate WARN conditions. A non-profitable Stage-3 pre-close observation is informational and cannot submit a SELL.

## Events that remain unthrottled

v3.9.0 does not coalesce away safety-critical evidence. These remain immediate:

- broker order rejection or inactive no-fill state;
- terminal partial fill;
- fill or quantity mismatch, including possible overfill;
- broker/local reconciliation mismatch or unknown position ownership;
- storage fault, worker stall, watchdog recovery, or unexpected worker termination;
- failed order submission after durable intent;
- transition to `ERROR` or `MANUAL_REVIEW`;
- failed final market-data revalidation after a SELL confirmation;
- app-owned order cancellation/replacement failure; and
- any existing decision event whose purpose is to prove a trading transition.

## Persistence and restart behavior

The coalescer state is intentionally in memory. No new SQLite object or persisted setting is introduced. After a complete process restart, a still-active condition may produce a new entry event and begins a new summary interval. This avoids making strategy or restart recovery depend on diagnostic housekeeping state.

Existing v3.8.0 databases, cycle rows, settings, orders, executions, decision events, backups, checkpoints, market captures, and watchdog recovery files remain compatible.

## Regression coverage

Focused v3.9.0 tests cover:

- changing Stage-3 age text using one stable condition key;
- a 705-observation NBIS-style stale-ask sequence producing a bounded number of audit events;
- one-minute/five-minute safety cadence and 15-minute expected-wait cadence;
- structured reason counts and suppression evidence;
- condition recovery;
- GUI-only cached/non-price callbacks;
- immediate invalid evidence near the trigger;
- invalidation after first SELL confirmation;
- normal and anomalous native-order wait cadence, including return to the normal INFO cadence after a transient crossing clears;
- reconnect aggregation and one recovery event;
- repeated BUY preflight blockers and recovery; and
- live Price Data Monitor rendering of the current Stage-3 evidence state.

The full existing test, mutation, simulation, callable-coverage, and archive-validation gates remain part of the release process.

## Release verification

The finalized v3.9.0 source passed the following release gates:

- 1,211/1,211 pytest cases passed with `ResourceWarning` promoted to an error;
- all 126 test modules passed when executed independently in fresh pytest processes;
- 33 focused v3.9.0 coalescing, GUI-status, metadata, compatibility, and release tests passed;
- 78.1% combined statement/branch coverage;
- 1,024/1,024 executable application callables entered;
- 17/17 targeted safety mutants killed;
- 58/58 deterministic simulation contracts passed across 54 CSV price paths;
- Python compilation passed for the application, tests, scripts, and `main.py`; and
- Git whitespace validation passed.

Ruff and Pyright were not installed in the offline Linux release environment and are therefore not recorded as passing. The Windows `run_all_tests.bat` quality gate remains required before creating a native executable. A native Windows/PyInstaller build and live IB Gateway smoke test were not performed in this environment.
