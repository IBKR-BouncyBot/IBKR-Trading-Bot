# v4.1.0 GUI metrics and audit layout

**Release:** v4.1.0 (same-version GUI-layout correction)

**Original baseline:** corrected v4.0.0 ATR close-window-save source

**Correction baseline:** initial v4.1.0 GUI metrics/audit layout source; all non-GUI runtime files remain byte-identical to it.

**Scope:** read-only GUI and reporting changes; no strategy or database migration.

## Live status and monitor

The Trading status box adds a right-aligned current displayed price / minimum-profit trigger pair. The existing trading eligibility text, tooltip and semantic color remain. The pair is not a SELL authorization: cached display prices can coexist with failed field-freshness, RTH, spread or confirmation checks. Read the Data/RTH indicators and detailed evidence as before. Price/trigger values unavailable in the current snapshot show a dash instead of a guessed value.

Position adds the cost of the remaining app-owned inventory. It does not use a broker account-wide position or market price. The ten status boxes now use equal stretch and ignore content-driven minimum widths; long values wrap instead of widening Trading or Position. The lock retains its compact width. The status row appears first, then the single five-stage ribbon, then the tabs. Both rows remain fixed outside the scroll area and update even when another tab is selected. LIVE Profile remains amber; failure indicators retain red.

Price data monitor now has five cards: Selected price, Source, Data mode including update age, Bid / Ask / Spread, and RTH status including current UTC and system time. The RTH/time card spans two columns in the lower row. Raw API fields remain expandable, but the adjacent explanatory sentence is removed. The five Field/Value pairs fill the available width with stretching Value columns; refresh and show/hide reapply those header modes.

The market/strategy section removes only the duplicate Stage card. Current stage and Why not moving? continue updating inside a collapsed Show stage details panel. The stage ribbon, top Stage box, live evidence messages, broker restrictions and button enablement remain. Simple hides the Recovery / audit log panel; Advanced/Debug and the separate Reconciliation tab retain their tools. No logging is stopped or deleted by hiding a panel.

The Strategy input map has a 420-pixel minimum height rather than 560, with smaller blocks and row gaps. All sixteen blocks, values, percentages, lane labels and explanatory text remain; no calculation is changed.

## Cost definitions

Total buy cost (order/position panel) and Invested (history column) are:

`actual cumulative BUY-filled quantity * average BUY fill price + recorded BUY commission`

Partial BUYs therefore show actual acquired cost, not the requested quantity or budget. Signed recorded fees/rebates are retained. Missing or invalid fill facts show unavailable; there is no substitution from budget or broker average cost. Commissions still pending from IBKR can subsequently change the amount.

The top Position amount allocates that entry cost, including BUY fees, proportionally to the app-owned shares still held. Mirrored protective/final SELL cumulative quantities are not subtracted twice. Completed/fully exited cycles show zero remaining position cost while history retains the full BUY cost. These are presentation values, not tax-lot cost basis, mark-to-market exposure, trading budgets, or new order-sizing inputs. Amounts use the portable database's existing USD/EUR display currency.

OrderRef uses the surrounding metric card background, borderless text area and proportional value font in both themes instead of inheriting the dark monospace log style. It is wrapped read-only plain text, including within long unbroken identifiers. Copying preserves the exact reference. An unusually long reference can scroll inside its own field; it does not force the entire order column wider or silently elide the identifier.

## Trade history

The completed summary has twelve cards, arranged in three columns and four rows. Average net P/L follows Best net P/L and Worst net P/L; it is total realized net P/L divided by the completed-cycle count in the existing summary; losing cycles and recorded commissions are included. It shows a dash for no completed cycles or missing summary data. It uses the same summary scope as the other eleven cards, not a new aggregation over display-only sample rows.

Invested follows Budget and sorts using its raw numeric value. Row-to-cycle identity remains unchanged when sorting/filtering and opening an audit. Existing CSV export schemas are unchanged; the added invested amount is derived for GUI display only.

The history viewport keeps a small fixed minimum height rather than growing with the number of rows. Its horizontal scrollbar remains reserved at the visible table bottom; extra rows scroll vertically. Individual column widths remain content-sized and bounded.

## Cycle audit

The Timeline uses linked crosshairs: hovering either plot draws a vertical guide at the same time in both. The hovered plot keeps its existing cursor-price guide and record tooltip. The counterpart horizontal guide uses its nearest actual plotted record on its own price scale, not the hovered graph's cursor price. The counterpart tooltip separately labels linked time and nearest record time, so a sparse action is not represented as an execution at the cursor time. With no counterpart record, only the time guide is drawn. Moving into the gap or leaving clears both. This is implemented only in CycleTimelineWidget; the single-graph Summary and the other custom chart classes keep independent hover behavior. Zoom/scroll still use the common time axis, and each repaint calculates linked coordinates from the current plot geometry without persistent linked mouse state.

The lower transition and risk tables retain their 3:2 horizontal allocation and stretching Message columns, but now have identical 210-pixel heights and independent scrolling. Decision events is an expanding table filling its lazy-loaded tab in both directions; its Message column absorbs spare width. Orders and Executions retain top-aligned layouts, with the final OrderRef column stretching across spare horizontal space.

Market capture starts at the top even when no capture rows are available. Its summary Value and preview Capture ZIP columns stretch to fill the width. An empty-capture page has a trailing spacer rather than vertical centering. The summary and file list remain bounded; a populated preview can use the remaining height. Each has its own scrollbar, without an outer page-level vertical scroll area.

## Upgrade and safety boundary

Keep the existing v4.0.0 or v4.1.0 database. There is no schema, cycle-state, settings, CSV or order-reference migration. In particular the last-five-minutes/session-close/orderly-close ATR saving policy is unchanged; an upgrade does not require a new warm-up when a valid matching checkpoint already exists.

Runtime changes are confined to `app/gui.py`; all non-GUI application modules and `main.py` are byte-identical to the baseline. Version 4.1.0, dependency requirements and build metadata remain unchanged; tests and documentation are updated separately. No IBKR order was sent during automated verification.

## Validation

Measured results and exact commands are recorded in `../IMPLEMENTATION_TEST_REPORT.txt`. The automated GUI tests use observable Qt doubles and recorded painter calls. They validate values, layouts/policies, sorting, read-only behavior and local/linked hover coordinates, not native screen rendering. Native Windows, real Qt/DPI rendering, packaged executable behavior and paper-account integration must be checked on a Windows machine before unattended live deployment.

Run `run_all_tests.bat` before `build_windows.bat`. Preserve input-lock, stale-worker, broker and reconciliation controls during visual smoke testing.

## Same-version correction verification

The earlier v4.1.0 test report is preserved in `legacy/V4_1_0_INITIAL_IMPLEMENTATION_TEST_REPORT.txt` as historical evidence, not as verification of this corrected archive. Current measured results, exact baseline hashes and remaining native Windows checks are in `../IMPLEMENTATION_TEST_REPORT.txt`.

## Status-row and linked-hover follow-up (same version)

This follow-up changes only the order of the two fixed header rows and the audit Timeline hover overlay. All earlier same-version table, style, metric and compact-layout corrections remain. The prior correction report is archived as `legacy/V4_1_0_LAYOUT_CORRECTIONS_IMPLEMENTATION_TEST_REPORT.txt`; current verification is in the root `IMPLEMENTATION_TEST_REPORT.txt`.
