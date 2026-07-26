# v3.2.2 GUI information, audit layout, and BouncyBot branding

**Release:** v3.2.2
**Initial release date:** 2026-07-25
**Same-release branding update:** 2026-07-26
**Type:** GUI presentation, application branding, packaging, and documentation maintenance

## Summary

v3.2.2 improves the information density of the Live Strategy price monitor,
uses the available Cycle Audit width more effectively, and applies the supplied
BouncyBot visual identity throughout the source and Windows build. The strategy
state machine, order construction, risk controls, broker reconciliation, SQLite
schema, session handling, and execution accounting are unchanged.

## Price data monitor

The price data monitor now shows three instrument lines on its left side:

1. the ticker symbol in the same large style as the current price;
2. a smaller contract-information line;
3. the current selected strategy price.

The smaller line can contain the IBKR long name and the exact qualified contract
identity, including security type, SMART route, primary exchange, currency,
`conId`, local symbol/trading class when useful, and the available IBKR industry,
category, and subcategory values.

The long name and classification data come from the same `ContractDetails`
response already required for order-type, market-rule, and session validation.
No additional recurring market-data request is introduced. When IBKR does not
provide descriptive fields, the monitor still shows the exact contract identity
that is available.

## Cycle Audit timeline

The two tables below the Timeline graph now size every column from its contents
instead of stretching a final column across unused space. Extremely long text is
capped and remains available through horizontal scrolling.

The stage-transition table receives the remaining horizontal space. The guard
and risk table uses a bounded content width and no longer automatically consumes
half of the dialog.

## Orders, Executions, and Decision events

The record tables on these three tabs now start at the top edge of the tab.
Compact tables no longer appear vertically centered. Larger record sets remain
scrollable inside the bounded table area.


## BouncyBot application branding

The supplied BouncyBot logo is stored as `Images/BouncyBot_logo.png`. A square
application mark derived from the same artwork is stored as
`Images/BouncyBot_app_icon.png`, with a multi-resolution Windows icon at
`Images/BouncyBot_app_icon.ico`.

The source application and main window load the icon through a resource-path
helper that works both from the repository and from a PyInstaller one-directory
bundle. The Windows build uses the `.ico` file for the executable and bundles the
PNG assets for runtime display. The versioned release folder also copies the
`Images/` directory so its README image links remain valid.

## About > Info

A new **About > Info** dialog shows:

- the BouncyBot logo;
- the current v3.2.2 product name and version;
- a clickable link to the project GitHub repository;
- the IBKR referral link and the Cardano, Midnight, Ethereum, and Solana support
  addresses from the root README; and
- a compact reminder that the software can transmit live orders.

Support addresses are presented in read-only selectable fields so they can be
copied without editing them.

## README and documentation maintenance

The project logo now appears immediately below the root README title. The
`Thank me` section was normalized for readable links, copyable addresses, and
correct spelling. Superseded v3.0.17, v3.0.18, and v3.0.19 release-note files no
longer remain in the root of `docs/`; the retained copies are indexed under
`docs/legacy/`. The current v3.2.2 note is the only release-specific document in
the root documentation directory.

## Compatibility

v3.2.2 adds no SQLite table, column, index, or data migration. Existing v3.2.1
portable databases, settings, active cycles, orders, executions, audit events,
and market-capture files remain compatible.

The only non-GUI data-flow addition is read-only contract-description metadata
attached to the existing in-memory price snapshot. It is not persisted as
strategy state and cannot affect an order decision.

## Verification

The release adds focused regressions for:

- long-name and exact-contract formatting in the price monitor;
- IBKR `ContractDetails` description and classification propagation;
- equal large-font styling for ticker and current price;
- content-sized Timeline columns and asymmetric table allocation;
- top-aligned Orders, Executions, and Decision events tabs;
- source and bundled asset resolution for the application icon and About logo;
- About-menu content and parity with the README support information;
- PyInstaller icon/data arguments and release-folder image copying;
- migration of superseded v3.0.x notes to `docs/legacy/`; and
- unchanged strategy, broker, database, and simulation behavior.

The final same-release source verification executed **1,041/1,041** pytest cases, measured **77.9%** combined statement/branch coverage, entered **927/927** executable application callables, killed **17/17** targeted safety mutants, and passed **58/58** deterministic simulation contracts across 54 CSV paths.

The complete release-gate results are recorded in the root
`IMPLEMENTATION_TEST_REPORT.txt` and in the packaged verification report.
