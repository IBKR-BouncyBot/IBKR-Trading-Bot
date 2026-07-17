# v2.12 live-tab controls and cycle-audit timeline cleanup

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

## Changes

- The bottom command/view-mode bar is now shown only while the Live strategy tab is selected.
- The cycle audit timeline now aligns BUY/SELL markers to visible capture rows by timestamp when possible, then by price for imported legacy rows with missing or incompatible timestamps. Unmatched ANCHOR, DROP, BUY, protective SELL, and FINAL SELL markers fall back to stable semantic stage positions.
- Captured price rows, BUY/SELL markers, stage transitions, and guard events now share one real horizontal timestamp axis. Imported rows with missing timestamps fall back to labelled positions rather than being treated as precise time data.
- Marker labels are clamped within the plot area and nudged apart to avoid overlapping the Y-axis and adjacent labels.
- Risk/guard blocks are filtered more strictly so successful imported cycles do not show red blocks merely because legacy diagnostic text contains the word error.

## Retained Windows build rollback and development launcher

- Added `run_dev.bat` at the project root.
- Added `scripts/run_dev.bat` beside `scripts/run_dev.ps1`.
- Both batch launchers use `powershell.exe -ExecutionPolicy Bypass -File ...` so the development GUI can be started without administrator rights or a permanent execution-policy change.
- `scripts/run_dev.ps1` now forces `QT_QPA_PLATFORM=windows` before launching the GUI and clears test-only variables:
  - `QT_QPA_FONTDIR`
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD`
  - `IBKR_BOT_HEADLESS_SIGNALS`
- `scripts/build_windows.ps1`, `scripts/build_windows.bat`, and `scripts/build_windows_checked.ps1` were reverted to the v2.2-style Windows build flow after later test-gated builds were interrupted before PyInstaller packaging on Windows.
- `scripts/run_tests.ps1` remains available for running the full test/simulation suite separately.

## Reason

A normal development GUI launch can inherit `QT_QPA_PLATFORM=offscreen` or `IBKR_BOT_HEADLESS_SIGNALS=1` from an earlier test command in the same PowerShell process. That can make the app start with test-only Qt/controller settings and produce misleading Qt messages such as an offscreen plugin warning.

## Expected Windows use

```bat
cd <unzipped>\\ibkr_trading_bot_v2_12
run_dev.bat
```

or:

```powershell
cd <unzipped>\\ibkr_trading_bot_v2_12
.\\scripts\\run_dev.ps1
```
