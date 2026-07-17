@echo off
setlocal
rem Launch the source GUI without elevation; ExecutionPolicy Bypass applies only to this PowerShell process.
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set PYTHONDONTWRITEBYTECODE=1
set QT_QPA_PLATFORM=windows
set QT_QPA_FONTDIR=
set PYTEST_DISABLE_PLUGIN_AUTOLOAD=
set IBKR_BOT_HEADLESS_SIGNALS=
set IBKR_BOT_NO_PAUSE=1
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_dev.ps1" %*
set RUN_DEV_EXIT_CODE=%ERRORLEVEL%
echo.
pause
endlocal & exit /b %RUN_DEV_EXIT_CODE%
