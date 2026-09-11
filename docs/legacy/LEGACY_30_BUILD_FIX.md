# legacy release Windows build warning handling

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

legacy release fixes a Windows PowerShell build failure where PyInstaller wrote a warning to
stderr and PowerShell treated that stderr line as a `NativeCommandError` because the
build script uses `$ErrorActionPreference = "Stop"`.

Observed symptom:

```text
pyinstaller.exe : ... DEPRECATION: Running PyInstaller as admin is not necessary nor sensible.
NativeCommandError
```

The PyInstaller process can still be successful in this situation. The corrected
script launches PyInstaller through `Start-Process`, redirects stdout and stderr to
log files, and treats the build as failed only when:

- PyInstaller exits with a non-zero exit code, or
- `dist\IBKRTradingBot\IBKRTradingBot.exe` is not created.

The warning should still be avoided in normal use by running the build from a
non-administrator PowerShell terminal, but it no longer stops the script merely
because PyInstaller wrote a warning to stderr.
