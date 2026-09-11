# v3.0.7 Windows build result handling

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

The Windows build produced a valid executable, but the PowerShell wrapper reported a red PyInstaller failure afterward.

`Invoke-PyInstallerLogged` used `Tee-Object` without consuming its success-stream output. PowerShell functions return every object written to that stream, so the caller received an array containing all PyInstaller log lines followed by the real exit code `0`. Comparing that array with zero produced a false failure.

The helper now pipes the displayed log through `Out-Host` and returns a single typed integer exit code. PyInstaller output remains visible and is still saved in `build_pyinstaller.log`. Successful packaging proceeds normally; genuine non-zero exits and missing executables still fail the build.

This patch changes Windows build result handling and version metadata only. It does not change trading logic, broker-order behavior, optional account routing, persistence, or GUI behavior.
