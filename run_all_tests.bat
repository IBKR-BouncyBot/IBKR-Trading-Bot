@echo off
setlocal
rem Full Windows validation and required quality checks: compilation, every pytest test (including bounded soak tests) with
rem ResourceWarning failures and line/branch coverage, per-callable coverage for app and main.py, safety mutation smoke tests,
rem CSV simulations, Ruff, and Pyright. No pytest marker filter is applied.
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set PYTHONDONTWRITEBYTECODE=1
set QT_QPA_PLATFORM=offscreen
set QT_QPA_FONTDIR=
set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
set IBKR_BOT_HEADLESS_SIGNALS=1
set IBKR_BOT_NO_PAUSE=1

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_tests.ps1" %*
set TEST_EXIT_CODE=%ERRORLEVEL%
if not "%TEST_EXIT_CODE%"=="0" goto done

set PYTHON_EXE=%~dp0.venv\Scripts\python.exe
if not exist "%PYTHON_EXE%" goto quality_python_missing

"%PYTHON_EXE%" "%~dp0scripts\run_quality_checks.py" --require-tools
set TEST_EXIT_CODE=%ERRORLEVEL%
if not "%TEST_EXIT_CODE%"=="0" goto quality_failed

echo.
echo QUALITY CHECKS PASSED.
goto done

:quality_failed
echo.
echo QUALITY CHECKS FAILED. See Ruff/Pyright output above.
goto done

:quality_python_missing
echo Could not find .venv\Scripts\python.exe after test setup.
set TEST_EXIT_CODE=1

:done
echo.
pause
endlocal & exit /b %TEST_EXIT_CODE%
