# v4.2.0 status headers, independent Timeline cursors and table sizing

**Release:** v4.2.0

**Baseline:** the supplied v4.1.0 Status Order Linked Crosshairs source archive.

## Changes

- In the fixed status row, the Trading price/trigger text and Position cost move to the top-right of their boxes, beside the top-left titles. Both sides use the same title font. The main status and share quantity remain below the header.
- In Cycle audit log > Timeline, cursor synchronization is removed. Hovering either graph draws its local crosshair and tooltip only in that graph. The other graph does not show a linked cursor. Moving between plots or leaving the canvas clears the previous hover overlay.
- Timeline data, separate price scales, shared time-axis mapping, zoom/scroll, chart overlays and Timeline tables retain their existing behavior. Summary and other charts retain independent hover behavior.
- The Reconciliation comparison table fits all eight rows at their wrapped heights. Its state/action columns stretch with the available width, and it recalculates row height after resize, font, style or data changes. The table has no internal scrollbars; on a short window, the page scrolls to keep its content and actions reachable. The four guided actions use a two-by-two grid, and the advanced action box no longer has a fixed maximum height.
- The Cycle audit Summary details table also refits when its width, font, style or data changes. Its existing height cap remains, with vertical scrolling available when wrapped content exceeds that cap.
- Shared compact-table fitting accounts for horizontal-scrollbar height so the scrollbar does not cover the last row. Full-row fitting supports uncapped tables; capped tables retain access to overflowing rows.

## Compatibility and scope

Runtime changes are confined to `app/gui.py`. This release introduces no strategy or database migration, no new settings, and no changes to broker actions, ownership, fill handling, recovery or watchdog behavior. Existing v4.1.0 databases remain compatible. The last-five-minutes/session-close/orderly-close ATR saving policy is unchanged. CSV export schemas are unchanged.

The existing GUI metrics remain reporting values. Total buy cost uses recorded BUY commission; Position reports the remaining app-owned inventory cost and is not tax-lot accounting. The twelve history summary cards retain their three columns and four rows.

Application, package and Windows build versions are 4.2.0. Dependencies are unchanged. The previous [release note](V4_1_0_GUI_METRICS_AND_AUDIT_LAYOUT.md) and [implementation/test report](V4_1_0_STATUS_ORDER_LINKED_CROSSHAIRS_IMPLEMENTATION_TEST_REPORT.txt) are preserved as historical material; their linked-crosshair behavior is superseded.

## Verification

Current measured results and any unavailable checks are recorded in [`../IMPLEMENTATION_TEST_REPORT.txt`](../IMPLEMENTATION_TEST_REPORT.txt). Previous-release pass counts are not evidence for this archive.

The relevant GUI checks cover top-right Trading/Position placement and matching title styling, local Timeline hover in both directions, unchanged data/scaling, hover cleanup, all-eight-row Reconciliation sizing, refitting after layout/content changes, and reachable overflow in capped tables. The separate `scripts/check_v410_gui_real_qt.py` smoke script checks actual widgets when real GUI dependencies are available; its historical filename is retained.

The behavior above is established by source inspection and the verification recorded in the root report, not by rendered Windows screenshots. Real Qt/native DPI tests are unavailable on this release host. Native Windows packaging and real Qt/DPI appearance still require the checks in [`TEST_PLAN.md`](TEST_PLAN.md); the changes do not constitute a claim that every screen is verified at every scaling factor. Headless Qt contract tests alone do not establish pixel geometry, Windows scaling or live IBKR integration.
