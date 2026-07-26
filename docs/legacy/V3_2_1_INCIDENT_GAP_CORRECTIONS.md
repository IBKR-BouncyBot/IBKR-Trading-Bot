# v3.2.1 production-incident gap corrections

**Release:** v3.2.1

**Release type:** focused correctness and audit-quality maintenance release

**Database schema:** unchanged from v3.2.0

## Summary

v3.2.1 resolves the three production-incident gaps that were retained as strict
expected-failure sentinels in the test-hardened v3.2.0 source:

1. timing-sensitive LSE/LSEETF actions now stop at the verified continuous-session
   boundary instead of using a later IBKR `liquidHours` endpoint;
2. repeated unchanged BUY preflight blockers are audit-throttled; and
3. a locally blocked BUY now records `PreflightBlocked` rather than the misleading
   `SubmitFailed` status.

The USD/EUR SMART-contract model, one-currency-per-database rule, broker order
handling, fill reconciliation, ten-second reconnect cadence, and all other
strategy parameters are unchanged.

## LSE and LSEETF continuous-session boundary

IBKR `liquidHours` remains the source for the contract's date-specific trading
day, holiday state, and any earlier close. For primary exchanges `LSE` and
`LSEETF`, BouncyBot additionally applies the independently verified London Stock
Exchange continuous session (08:00-16:30 Europe/London):

```text
Time zone: Europe/London
Continuous open: 08:00
Continuous close: 16:30
```

In other words, the verified LSE/LSEETF continuous window is
**08:00-16:30 Europe/London** on an ordinary trading day.

The effective session is the intersection of those two windows:

```text
effective open  = later of IBKR open and 08:00 London
effective close = earlier of IBKR close and 16:30 London
```

This means the policy can only narrow IBKR's session; it can never extend a
holiday, late open, or early close supplied by IBKR. If the policy cannot be
applied safely, the contract fails closed.

The corrected effective boundary drives:

- the RTH open/closed state;
- first- and last-minutes BUY guards;
- active BUY cancellation before close;
- Stage-3 profitable liquidation before close;
- Stage-4 trail cancellation and liquidation before close; and
- RTH-only ATR observation.

The RTH status retains both the original IBKR boundaries and the effective
continuous-session boundaries for diagnostics. A 30-second RTH metadata cache
can no longer keep a contract marked open after the effective close has passed.

This venue override is intentionally limited to `LSE` and `LSEETF`, for which the
available production incident and exchange-session evidence exists. It is not an
independently maintained LSE holiday, special-session, or early-close calendar:
IBKR's date-specific `liquidHours` remains authoritative whenever it opens later,
closes earlier, or reports the day closed. Other SMART primary exchanges continue
to use their exact IBKR `liquidHours` metadata unless a separately verified
continuous-session policy is added in a future release.

## Audit throttling for unchanged BUY preflight blocks

A persistent local blocker, such as delayed market data in a live profile, can
remain true over many strategy cadences. v3.2.0 correctly refused the BUY but
could write the same warning to SQLite on every attempt.

v3.2.1 stores the first warning immediately and then limits the same blocker
category for the same cycle to one audit event per 60 seconds. The cycle state is
still kept current, and the order remains blocked for every evaluation. Only the
redundant audit-event volume is reduced.

The throttle key is stable and does not contain changing price values. The cache
is bounded, and the first warning is emitted even when a deterministic monotonic
clock starts at zero.

## `PreflightBlocked` status

When a BUY is prevented before a live broker order is submitted, BouncyBot now
records:

```text
PreflightBlocked
```

Examples include:

- delayed or unconfirmed live market data;
- stale data;
- session timing or RTH closure;
- configured hard-risk limits;
- invalid local price or quantity normalization;
- failed what-if validation; and
- unavailable submission prerequisites.

`SubmitFailed` remains reserved for an actual order-submission attempt that
raises before broker acceptance can be confirmed. No order intent is written for
a `PreflightBlocked` action.

## Compatibility

v3.2.1 adds no SQLite table, column, or index. Existing v3.2.0 databases open
without migration. Status columns are text fields and can store the new
`PreflightBlocked` value without schema changes.

The release remains restricted to one contract currency per database and to
exact API-selected USD/EUR ordinary `STK` contracts routed through SMART.

## Verification scope

The three former strict expected failures are ordinary passing regressions in
v3.2.1. Focused tests cover:

- the VWRA/LSEETF 16:30 London continuous close;
- preservation of an earlier IBKR close;
- preservation of a later IBKR open;
- London winter-time conversion;
- unchanged behavior for non-LSE contracts;
- cached RTH status closing at the effective boundary;
- fail-closed malformed continuous-session metadata;
- immediate first-event logging at monotonic time zero;
- 60-second audit throttling and re-emission after the interval; and
- `PreflightBlocked` persistence without an order intent for both trailing and
  zero-trail market BUY actions.

The safety mutation smoke gate includes dedicated mutations for the 16:30 LSE
close, the `PreflightBlocked` status, and first-event logging at monotonic time
zero.

Final offline verification executed **1,026/1,026** collected pytest cases with no expected failures, measured **77.7%** combined statement/branch coverage, entered **921/921** executable application callables, killed **17/17** targeted safety mutants, and passed **58/58** deterministic simulation contracts across 54 CSV price paths. Python/AST and documentation-link audits covered 147 Python files, 86 Markdown files, and 309 local links.

As with earlier releases, offline tests do not replace a Windows packaged-build
smoke test or an IBKR paper-account test. A representative LSE/LSEETF paper
contract should be checked around the continuous close before production use.
