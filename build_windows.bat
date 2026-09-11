@echo off
setlocal
set IBKR_BOT_NO_PAUSE=1
powershell -ExecutionPolicy Bypass -File "%~dp0scripts\build_windows.ps1" %*
set BUILD_EXIT_CODE=%ERRORLEVEL%
echo.
pause
endlocal & exit /b %BUILD_EXIT_CODE%
