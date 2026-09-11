# v2.12 Windows runtime and development launcher cleanup

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

This revision keeps the development launcher cleanup and reverts Windows packaging to the v2.2-style build path.

- `scripts/run_dev.ps1` now clears test-only/headless settings before starting the app, explicitly sets `QT_QPA_PLATFORM=windows` when running on Windows, and points Qt at the normal Windows Fonts folder when available.
- `scripts/build_windows.ps1`, `scripts/build_windows.bat`, and `scripts/build_windows_checked.ps1` were reverted to the v2.2-style Windows build method. The default packaging build no longer runs pytest before PyInstaller.
- `scripts/run_tests.ps1` remains the separate full test/simulation runner.
- `scripts/run_dev.bat` launches the development PowerShell script with `ExecutionPolicy Bypass` for that process only. It does not require administrator rights and does not change the machine or user execution-policy setting.
- A root-level `run_dev.bat` is also included for double-click/convenience use from the extracted project folder.

The Qt messages seen after running a development script were warnings, not a trading-logic crash. v2.12 keeps the non-admin development launchers and the normal-GUI environment cleanup while reverting the Windows build script to the previously working v2.2-style packaging flow.
