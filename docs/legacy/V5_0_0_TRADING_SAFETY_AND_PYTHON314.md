# v5.0.0 targeted trading safeguards and Python 3.14

**Release:** v5.0.0. This release implements the necessary findings from the reassessment of the corrected 4.2.0 source. It preserves the five-stage strategy and existing native-order model while adding checks at broker submission, recovery and replacement boundaries.

## Trading and recovery changes

- A possibly transmitted order is retained as `SUBMISSION_UNKNOWN`, with its exact OrderRef and any returned broker IDs. The cycle enters persistent `MANUAL_REVIEW`; reconnection alone never authorizes a duplicate submission. Definite pre-transmit errors retain the ordinary rollback behavior.
- Cancellation requires exact OrderRef and current-client identity. Numeric order ID alone is no longer ownership evidence.
- Recovered ordinary SELLs use the same exact-quantity checks as live polling. A terminal partial exit cannot complete the cycle or start a repeat. Recovered holdings must cover reconciled app-owned unsold quantity for the exact account and contract; unrelated extra holdings are permitted.
- Late executions for replaced orders resolve through persisted order identity. The execution ledger remains idempotent, and replacement orders are checked against the newly remaining exposure. Substantive partial-BUY rejection stops automatic repeat while acquired shares remain managed.
- Active-cycle orders use the confirmed cycle account and exact contract. New cycles cannot replace another unresolved cycle. Historical blank accounts require unambiguous owned broker evidence when exposure already exists; ambiguous recovery pauses visibly.
- A rejected account selection for a never-started cycle leaves no phantom recovery cycle or account lock. The operator can select a confirmed managed account and retry; existing exposure retains strict account recovery checks.
- Restored Stage 1 cycles bind a blank legacy account before generating their first BUY reference. Existing references, fills and submission uncertainty retain exact ownership requirements. Previously persisted manual review is not cleared automatically; verified pre-submission failures use the existing explicit manual-handling workflow.
- Read exact persisted execution evidence from the older `Fill(...)` representation as well as current structured metadata when proving a legacy cycle's account. Execution/order/contract identity and current managed-account checks still apply; no account is inferred from position size or single-account availability.
- Multiple-cycle blocks identify the affected cycles. Reconciliation provides a separate, explicit historical handling acknowledgement that preserves the current cycle and recorded fills, rechecks the confirmed historical snapshot, and never sends an order or resumes automatically. Resume checkpoints preserve unchanged execution identity while retaining validation in the unavailable-worker fallback; acknowledged rejections cannot bypass the worker.
- BUY guards use independent selected-price/bid/ask freshness, including settings edits and partial-BUY supervision. New market-data generations do not inherit freshness from cached prices or size-only events. Subscription replacement avoids overlapping streams for the same underlying ticker. Nonblocking polls preserve a pending fallback subscription until its first response instead of repeatedly cancelling/replacing it; bounded probes can still retry alternative requests.
- Trading status uses those independent quote ages without mislabelling a recent quote as stale between selected-price events. Strategy evaluation and order-entry checks remain unchanged. Reused generic subscriptions retain their requested tick metadata in audit snapshots.
- The configured spread ceiling applies independently of the hard-risk master and requires usable quote evidence. RTH uses IBKR ContractDetails `liquidHours` and `timeZoneId`; guessed weekday schedules and timezone substitutions are removed. Missing/invalid session evidence blocks restricted orders and produces no estimated GUI hours/countdown. The existing LSE session-narrowing policy remains.

## Protection and precision

When protective SELL is enabled, it is sized from the reconciled acquired quantity after the BUY becomes terminal. Local protection failure becomes a visible recovery condition. The app checks the normalized replacement SELL price, quantity and applicable guards before cancelling protection. It rechecks submission evidence afterward; a failure after cancellation cannot silently return to normal unprotected waiting. Failed cancellation is reconciled and made actionable rather than leaving a silent permanent wait flag. Separate cancellation and submission cannot guarantee an uninterrupted protective order.

Strategy comparisons retain the unrounded configured drop/profit thresholds, including recalculation from current anchors/fills. Display prices remain formatted normally. This prevents tiny low-price thresholds collapsing into immediate BUY/SELL actions without their configured movement.

## GUI and restore

The Protection indicator distinguishes confirmed working protection, cancellation in progress, missing protection and unconfirmed status. Advanced Connect follows the same eligibility and input lock as the workflow button.

Connecting a restored cycle loads its exact stored contract for quote monitoring before Start, including when the upstream IBKR link becomes available later. Qualification failures do not trigger repeated qualification at every strategy cadence, infer another contract, or resume trading. Successful resume replaces the obsolete startup instruction when no newer status or fault needs to be preserved. Old guard messages no longer label a running stage 2–4 as a BUY-blocked Start action; Stage 1 and real recovery gates remain unchanged.

Settings edits during the startup pause still save permitted fields but cannot evaluate BUY/SELL transitions from the displayed quote. The explicit Start gate applies to both normal price processing and edit-triggered reevaluation.

Saved ATR reuse and Stage 1 warmup rules are unchanged from 4.x. A valid saved estimate can supply readiness during an open RTH session while current-session bars accumulate. Without an accepted estimate, enabled ATR warmup blocking still pauses the initial-drop trigger.

The five stage indicator boxes use a 60-logical-pixel minimum, with reduced vertical padding. Their 14-pixel text size remains unchanged; wrapping can still require more height. Trading/Position details occupy the top-right of their boxes in the title font. Timeline cursors are independent. The Reconciliation comparison table fits all eight rows, refits on resizing/font/style/data changes and uses page scrolling when the window is too short.

Restore validation checks original required columns, primary keys and declared foreign-key integrity, then exercises supported additive migrations on a disposable consistent SQLite backup. It does not modify the candidate or active database. Later additive columns may be absent in a valid older backup. This is a restore-readiness check, not broker reconciliation or a general validation of every stored value.

## Audit loading and post-fill market context

Opening a Cycle audit log starts one read-only background worker after Summary appears. It prepares Decision display values and streams the completed capture ZIPs; Timeline and Market capture share that result. Qt widgets remain on the GUI thread. Decision cells and wrapped row heights are filled in batches capped at 40 rows and a short time budget, with bounded column-content sampling. Tab clicks during loading reuse the same job. Closing the dialog cancels remaining work, ignores late completion and releases the modal widgets.

A capture's manifest and fill-event payload must match the selected cycle and instrument before cross-cycle market context is accepted. Rows must be within the verified recorded/declared window and identify the same ticker and contract. This preserves the post-SELL price path when Auto-repeat changes the active cycle ID. Contradictory instrument data, unrelated archives and out-of-window records remain excluded; incomplete legacy captures keep strict cycle filtering. Capture recording and broker/trading behavior are unchanged.

Timeline's stage-change markers/table now require two nonblank, different stages. Repeated ATR updates remain in Decision events. The disconnected Account box shows `N/A` when no account is known, while known saved/cycle accounts remain visible.

The supplied NBIS cycle 37 is a regression reference: both captures contain 30,357 rows in total, including 7,371 same-instrument post-SELL rows tagged cycle 38. The SELL path extends through 20:10:05 UTC on 14 August 2026. Decision events retains all 969 records; Timeline shows three actual stage changes. The private database/captures are not distributed with the source.

## Python migration and installation

Source launch, test and build scripts require **standard GIL-enabled CPython 3.14.x**. They select `py -3.14` on Windows and verify the actual interpreter before installing packages or launching. The free-threaded 3.14t build and other Python branches are rejected by those scripts.

An existing `.venv` retains its original interpreter. Close the app, retain its database/backups and working executable, then rename the old environment (for example to `.venv-pre-5`) before creating the new `.venv`. Do not overwrite a running installation. Use:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\run_all_tests.bat
.\scripts\build_windows.ps1 -RunTests
```

`run_all_tests.bat` is the complete Windows validation gate. The build's `-RunTests` option runs pytest and CSV simulations only; it does not replace the coverage, callable, mutation or quality gates in the full test runner.

The dependency floors now cover Python 3.14 support, including PySide6 6.10.1+, pytest 8.4.2+, coverage 7.10.7+ and Ruff 0.14+. PyInstaller remains 6.21+ and ib_async remains 2.1.0+. Most requirements retain upper bounds; `tzdata` specifies a minimum only. These constraints are not a deployment lockfile; record the exact installed versions when qualifying a deployment. Installing a new system Python does not change an already-built portable executable.

## Compatibility and verification boundary

The patch uses existing database tables/status fields and does not add a new trading architecture. Existing supported databases follow the additive opening path. Market orders, native trailing orders, ATR persistence, next-order settings and partial-BUY grace remain subject to their existing rules plus the new safety checks.

The source release includes focused regression tests for the changed fault paths and adjacent successful paths. Consult the root `IMPLEMENTATION_TEST_REPORT.txt` for measured counts, independent review and remaining gates. The available host could not download/run Python 3.14 or run native Windows/Qt, actual pytest, coverage, Ruff or Pyright. Offline checks on Python 3.12 and a limited compatibility runner must not be interpreted as those gates passing. A Windows build and paper-broker qualification remain required before live deployment.

The prior [4.2.0 note](V4_2_0_HEADER_AND_INDEPENDENT_TIMELINE.md) and [verification report](V4_2_0_IMPLEMENTATION_TEST_REPORT.txt) are retained unchanged in scope as historical records.
