# GUI review accompanying 5.5.1

**Resolution in 5.6.0:** the fifteen findings below are addressed by the targeted changes described in the [5.6.0 release note](legacy/V5_6_0_TARGETED_GUI_FIXES.md). The original findings and reproductions are retained here as historical evidence. For executed validation and remaining platform gates, use the current [implementation and test report](../IMPLEMENTATION_TEST_REPORT.txt).

Reviewed on 2026-10-04 against the exact released 5.5.0 source and the proposed 5.5.1 reporting changes. The review covered the main ribbon, price monitor and graph, live settings, command and lock controls, reconciliation, historical flowcharts, Trade history, CSV export and Cycle audit views/worker lifecycle.

The requested history-summary defect is fixed in 5.5.1. A blank ticker and default history filters now count all completed cycles in the database. Summary and table queries use the same filters before the table's 500-row limit. Clearing a ticker reloads the wider result, and delayed results for an obsolete filter cannot overwrite the current summary. These related filter corrections are included in the release.

**The 15 additional findings below are reported, not fixed in 5.5.1.** They are present in the 5.5.0 baseline; the reviewed 5.5.1 changes leave their affected logic unchanged. Their earliest introduction has not been established. They must not all be described as regressions introduced by 5.5.0.

P1 means a configuration issue that can affect trading settings and deserves the highest follow-up priority. P2 means incorrect operator guidance, action consistency or reporting. P3 means an incomplete or misleading lower-impact readout. This is a prioritized defect review, not a claim of native-platform or live-broker qualification.

| ID | Priority | Confirmed discrepancy |
| --- | --- | --- |
| G01 | P1 | A queued ATR snapshot can overwrite a newly entered manual percentage and autosave the replacement. |
| G02 | P2, action safety | Reconciliation market SELL omits the potential-loss confirmation used by Stop strategy. |
| G03 | P2 | Reconciliation can call a 100-share local position versus one broker share consistent. |
| G04 | P2 | A partially filled, nonterminal SELL is treated as nonworking by reconciliation display/control logic. |
| G05 | P2 | Confirming another ticker after a flat completed/stopped cycle can mix old/new ticker labels and graph data. |
| G06 | P2 | Estimated trailing stops use extrema from before the order existed. |
| G07 | P2 | Refresh/filter changes can silently switch the selected historical flowchart to another cycle. |
| G08 | P2 | Historical flowcharts inherit some current settings instead of their stored settings. |
| G09 | P2 | Manual protective-SELL percentage remains disabled after stop/completion. |
| G10 | P2 | CSV export ignores several selected history filters and uses different ticker matching. |
| G11 | P2 | An unfilled, cancelled protective order can cause a normal final exit to be labelled PROTECTIVE EXIT. |
| G12 | P2 | Timeline fill markers combine the first partial-fill price with the completion timestamp. |
| G13 | P3 | Audit Summary treats legitimate zero settings as missing. |
| G14 | P3 | Audit Summary omits holding duration and BUY order type despite available stored evidence. |
| G15 | P2 | Summary totals update while history rows remain unchanged until Refresh or a filter change. |

## G01 — ATR snapshot overwrites a manual percentage

**Condition and evidence:** switch off minimum-profit adaptation, enter 3.21%, then deliver a previously queued worker snapshot with adaptation enabled and an adaptive value of 9.99%. The GUI changes the input to 9.99%; autosave sends adaptation disabled together with 9.99%. This was reproduced with actual GUI methods and the repository Qt stubs. The controller's save-draft path persists/applies that payload. Corresponding overwrite paths exist for protective percentage and switching off ATR entirely.

**Cause:** `MainWindow._apply_atr_adaptive_snapshot_to_inputs` permits updates using snapshot adaptation flags, which may precede the operator's current checkbox selections. Its master-enabled expression also accepts either the snapshot or the UI as enabled. `_autosave_settings` subsequently collects the overwritten widget values.

**Impact:** a manual setting can differ from what the operator entered, including a minimum-profit value editable during Stage 3. The event ordering is timing-dependent. This reproduction is not evidence that it happened in the supplied live audit bundles.

**Targeted correction:** make the current UI selection and pending manual edits authoritative after initial hydration. Test disabling individual adaptations and master ATR while older snapshots remain queued.

## G02 — Reconciliation market SELL lacks the other entry point's confirmation

`MainWindow._recovery_sell_market_clicked` checks refresh freshness and immediately requests `SELL_APP_POSITION_MARKET`. The same action through `StopDialog._confirm_sell_market` displays an OK/Cancel potential-loss warning with Cancel as the default. The reproduction observed zero confirmation calls from Reconciliation.

An accidental Reconciliation click can therefore start the cancellation/liquidation workflow. Existing controller identity, position and RTH checks still apply; this does not bypass those guards.

**Targeted correction:** share the existing confirmation flow and recheck the current cycle/quantity/refresh after the modal dialog.

## G03 — Reduced broker position reported as consistent

`MainWindow._update_recovery_panel` compares a Stage-3 broker position with `min(1.0, open_qty)`. This checks coverage of at most one share. A synthetic current probe containing one broker share against 100 app-owned unsold shares produced “Recovery state is consistent with the current snapshot.”

This can hide a position discrepancy in operator guidance. The controller's waiting-cycle recovery separately compares the full quantity; the finding does not establish an unsafe SELL submission.

**Targeted correction:** compare against the complete app-owned unsold quantity with the existing numeric tolerance and distinguish unknown/stale broker quantities. Extra unrelated broker holdings should remain allowable.

## G04 — Partially filled SELL classified as nonworking

The local `status_working` helper inside `_update_recovery_panel` returns false when filled quantity is positive, even for Submitted, PreSubmitted or PendingCancel. The controller correctly bases working status on whether the order is terminal.

Reproduction: 100 bought, 20 protective shares filled, protective status Submitted, broker position 80, but no matching order in a current probe. Reconciliation reports consistency and disables the cancellation action. The same missing order with no partial fill correctly warns.

**Targeted correction:** align the GUI helper with the controller's terminal-status semantics. Cover live remainders and terminal partials separately.

## G05 — Old/new ticker labels and graph data mixed

After a flat completed/stopped cycle is retained, confirming another instrument updates the controller contract/price while retaining the old cycle. `LiveStatusBar.update_data` and `StrategyGraphWidget.update_data` prioritize that cycle; the price monitor prioritizes the new contract.

Reproduction: OLD priced at 110, then NEW priced at 25, yields OLD in the ribbon, NEW in the monitor, and both prices in the same graph with old cycle levels.

**Targeted correction:** use the currently confirmed contract identity for display/buffer identity when the retained cycle belongs to another instrument; omit mismatched historical levels. Preserve the cycle's recovery and ledger records.

## G06 — Trailing-stop estimates include pre-order prices

`StrategyGraphWidget._levels` calculates estimated stops from the complete rolling cycle price buffer. Earlier extrema can precede order submission. Buffer pruning can also remove an extremum relevant to an existing order.

Reproduction: Stage-1 price 150, then Stage-4 price 110 with a submitted initial SELL stop of 108.90 and a 1% trail. The graph displays an estimated SELL stop of 148.50. This estimate does not modify the actual broker order, but misrepresents its likely trigger and can distort the graph scale.

**Targeted correction:** use per-order post-submission evidence and retain extrema independently of display-buffer pruning. Where restart evidence is insufficient, mark the estimate unavailable.

## G07 — Historical flowchart selection changes identity

`FlowchartPanel.set_history_rows` preserves the selector's list index. Prepending/reordering/filtering rows then restores that index, which can identify another cycle.

Reproduction: select BBB from [AAA, BBB], then prepend CCC. The selection changes to AAA.

**Targeted correction:** preserve a persisted cycle ID, restoring that cycle only if still in the filtered results; otherwise deliberately return to the current cycle.

## G08 — Historical flowchart borrows current configuration

`FlowchartPanel._strategy_from_history_row` starts from the current strategy and replaces only selected fields. Persisted ATR, repeat/reinvestment and several guard settings are not all restored.

Reproduction: a stored cycle with ATR off, 9 bars × 300 seconds and repeat/reinvestment off renders current ATR on, 14 × 60 seconds and repeat/reinvestment on.

**Targeted correction:** reconstruct supported settings from the stored cycle and handle genuinely missing legacy values explicitly. Changing today's settings should not alter the explanation of a past trade.

## G09 — Protective percentage locked after stop/completion

`MainWindow._set_atr_percentage_field_state` allows manual protective percentage only with no stage, Stage 1 or Stage 2. It omits terminal stages that other next-cycle inputs allow.

With ATR off and a retained STOPPED, completed or ERROR cycle, the protective checkbox and other next-cycle inputs are editable but its percentage remains disabled.

**Targeted correction:** allow safe inactive stages while retaining the operator lock, ATR rules and active-position/order restrictions.

## G10 — CSV export differs from selected history

`MainWindow._export_history` forwards only ticker text. Date, outcome, ATR and paper/live filters are ignored. CSV storage uses exact ticker matching; history controls use literal substring matching.

Confirmed against the supplied ARGX database: a Losing filter selects one SHELL trade but CSV includes all three SHELL/TDIV trades. Ticker text SH matches SHELL in the table but exports no rows.

**Targeted correction:** use the same full-database reporting predicates for export, without the table's 500-row limit. 5.5.1 retains the existing CSV behavior and documents this boundary.

## G11 — Unfilled protective reference misclassifies the exit

`CycleAuditDialog._outcome_badge`, also used by Trade history, treats any protective order reference as evidence of a protective exit. A cancelled protective order with zero fills can remain on a cycle that later exits through the ordinary final SELL.

A valid synthetic profitable completed cycle in that state renders PROTECTIVE EXIT. Its Profit exit/Protective exit filtering is consequently wrong, although numeric P/L is unchanged. The supplied completed cycles do not demonstrate this condition; the reproduction uses a reachable synthetic state.

**Targeted correction:** use actual protective fills or the executing order identity. A reference alone does not prove execution. The 5.5.1 filter queries intentionally preserve existing classification until this separate defect is addressed.

## G12 — Timeline marker uses first partial-fill price at completion time

`CycleTimelineWidget._build_markers` prefers the first matching execution's average price while its marker timestamp can come from the completed cycle.

Confirmed in supplied PURR cycle 2: the FINAL SELL marker is 13.71 from the first partial execution, while the completed weighted average is 13.691500630517025 for 793 shares. The marker uses the completion timestamp. A corresponding BUY example exists in PURR cycle 3.

**Targeted correction:** use the cycle aggregate price for a completion marker, falling back only when aggregate evidence is absent; alternatively show individually timed execution markers. Persisted execution/P&L records remain intact.

## G13 — Valid zero settings displayed as absent

`CycleAuditDialog._summary_tab` uses chained boolean `or` expressions for values including initial drop, rebound, minimum profit, trailing percentage and slippage. Zero is therefore discarded.

A valid zero-valued synthetic cycle renders “-” or “Not available from audit rows”. Zero trail settings can represent market-order behavior, so this loses meaningful information.

**Targeted correction:** select the first available non-None value instead of the first truthy value.

## G14 — Audit Summary ignores available order type and duration

`CycleAuditDialog._summary_tab` expects precomputed order-type and holding-duration display fields. Production history enrichment does not supply them, although stored order rows and cycle timestamps are available.

Confirmed on supplied TDIV cycle 1: the BUY record says TRAIL and both fill timestamps exist, but Summary displays “TRAIL or MKT from stored order row” and duration unavailable.

**Targeted correction:** derive the BUY type from the corresponding stored order and the duration from valid BUY/SELL timestamps, retaining an explicit unavailable result where evidence is missing.

## G15 — Summary refresh and row refresh have different lifetimes

Periodic snapshots update summary cards. History rows and the historical flowchart selector refresh at startup, manual Refresh, and in 5.5.1 when filters change. Completing a trade or revising commission does not itself request new rows. Switching tabs only reapplies cached rows.

Thus a newer summary can coexist with an older table. This was established by tracing all history request/emission sites; no live completion was performed for this review.

**Targeted correction:** request rows when a completed-history revision changes, preserving filters and selection. Avoid unconditional frequent full-history queries.

## Verification and limits

The review combines source tracing, independent reviewers, deterministic GUI-method reproductions using the existing Qt stubs, read-only queries against the three supplied databases, and the existing audit-worker lifecycle tests. The 5.5.1 reporting fix was independently checked against 63 combinations of outcome/ATR/mode filters over 144 persisted synthetic fixtures, in addition to its regression tests.

No broker connection or real order was made. The supplied databases were read without modifying their contents. Reproduction artifacts use synthetic identities or limited trade metrics; account identifiers and user database files are not included in the release.

Native Windows Qt rendering, scaling, event-loop timing and packaged execution were not verified because the required toolchain could not be installed on this host. No additional concrete audit-worker lifecycle defect was established. These limits mean the scan is not proof that every GUI path is defect-free.
