# legacy release Windows build reliability fix

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

This version keeps the legacy release trading/runtime defaults but changes Windows packaging to be closer to the legacy release build path that worked reliably.

Changes:

- `scripts/build_windows.ps1` now uses `.venv\Scripts\pyinstaller.exe` first.
- The forced `--collect-submodules ib_async` scan was removed because direct imports are sufficient and the forced dependency walk can make PyInstaller analysis much slower on some Windows/Python installs.
- Full pytest and CSV simulation checks now run during packaging by default. Use `scripts\run_tests.ps1` for validation without packaging, or `scripts\build_windows.ps1 -SkipTests` only for a deliberate local packaging-only run.
- The source package excludes generated `__pycache__` files.

Normal build:

```powershell
.\scripts\build_windows.ps1
```

Checked build:

```powershell
.\scripts\build_windows.ps1
```

Clean virtual environment build:

```powershell
.\scripts\build_windows.ps1 -CleanVenv
```

## UI spacing fix

The price-data monitor uses a fixed table height based on actual row heights plus a small cushion. This keeps internal scrollbars hidden without creating a large blank gap below the RTH status row.
