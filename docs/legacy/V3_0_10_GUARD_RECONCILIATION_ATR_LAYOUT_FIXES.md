# v3.0.10 guard, reconciliation, ATR, and dashboard corrections

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

> [!NOTE]
> This is a historical release note for v3.0.10. For current behavior, use [`../../README.md`](../../README.md), [`CONFIGURATION_REFERENCE.md`](../CONFIGURATION_REFERENCE.md), [`RISK_CONTROLS.md`](../RISK_CONTROLS.md), and [`RECOVERY_AND_FAILSAFE.md`](../RECOVERY_AND_FAILSAFE.md).

## Maximum spread remains user controlled

The GUI previously reapplied a suggested hard-risk default whenever a new price snapshot arrived and the field still matched the previous suggestion. Because the suggestion accepted live bid/ask, the visible **Maximum spread %** threshold could change without an explicit edit.

v3.0.10 removes bid/ask from the GUI suggestion input and excludes Maximum spread from automatic suggestion writes. The live spread is still calculated and compared with the saved threshold. The threshold itself changes only through direct user input or loading persisted strategy settings.

## Completed cycles no longer inherit stale active-order presentation

The Reconciliation broker probe is a point-in-time snapshot. Normal order monitoring can receive a later terminal fill or cancellation. v3.0.10 updates/removes the matching cached probe row when that newer broker poll arrives. The GUI also defensively suppresses an older matching probe row when the local terminal timestamp is newer. A later explicit broker refresh is never suppressed.

Stop, the main-window close path, and Reconciliation now obtain unsold application quantity from the same persisted app-owned fill ledger. They do not infer app ownership from the account-wide broker position. Consequently, a fully sold completed cycle does not offer an unnecessary market SELL merely because unrelated shares of the same stock exist in the account.

## Configured guards are not recovery faults

ATR warmup, spread/data/session guards, and ordinary strategy waits can pause a BUY without creating a broker/local mismatch. v3.0.10 disables Resume, Stop, Cancel, Sell, Leave-working, and Mark-handled actions in that condition. Read-only broker refresh and audit export remain available. Recovery actions become available only when an independent app order, app-owned unsold quantity, recovery-required flag, or actual inconsistency exists.

## ATR diagnostics collect while adaptation is disabled

The controller already maintained an RTH-only observation buffer for ATR. v3.0.10 makes diagnostic bar/readiness calculation independent of the adaptation toggle. Turning adaptation off prevents ATR values from rewriting strategy percentages, but current-session RTH observations continue to accumulate. Collection pauses outside RTH, and the in-memory buffer is not restored after restart.

A short calculation cache prevents the unrestricted cached-subscription worker loop from rebuilding identical ATR bars more often than useful while retaining the bounded RTH observation history.

## Dashboard layout

The fixed five-button command bar remains the workflow control surface. The duplicate dashboard Controls group is no longer constructed, and **Recovery / audit log** occupies the full dashboard width in Simple, Advanced, and Debug modes.

## Regression coverage

The release adds focused checks for:

- no live bid/ask-derived Maximum spread write;
- full-width audit layout in every view mode;
- app-owned quantity use in Stop, close, and Reconciliation;
- ATR collection and readiness with adaptation disabled;
- guard-only recovery action gating;
- stale recovery-probe retirement after a terminal SELL poll;
- preservation of a later explicit broker probe that still reports an order.
