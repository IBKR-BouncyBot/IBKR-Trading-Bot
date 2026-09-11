$__IbkrScriptPauseSuppressed = [Environment]::GetEnvironmentVariable("IBKR_BOT_NO_PAUSE", "Process") -eq "1"
function Wait-IbkrScriptPause {
    if (-not $__IbkrScriptPauseSuppressed) {
        Write-Host ""
        Read-Host "Press Enter to exit" | Out-Null
    }
}
trap {
    Write-Error $_
    Wait-IbkrScriptPause
    exit 1
}

$ErrorActionPreference = "Stop"
$__IbkrTestEnvNames = @(
    "PYTHONUTF8",
    "PYTHONIOENCODING",
    "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE",
    "QT_QPA_PLATFORM",
    "QT_QPA_FONTDIR",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
    "IBKR_BOT_HEADLESS_SIGNALS"
)
$__IbkrTestSavedEnv = @{}
foreach ($name in $__IbkrTestEnvNames) {
    $__IbkrTestSavedEnv[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

function Restore-IbkrTestEnvironment {
    foreach ($name in $__IbkrTestEnvNames) {
        $oldValue = $__IbkrTestSavedEnv[$name]
        if ($null -eq $oldValue) {
            Remove-Item "Env:\$name" -ErrorAction SilentlyContinue
        } else {
            [Environment]::SetEnvironmentVariable($name, $oldValue, "Process")
        }
    }
}

try {
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONUNBUFFERED = "1"
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:QT_QPA_PLATFORM = "offscreen"
    if ($env:WINDIR) {
        $windowsFonts = Join-Path $env:WINDIR "Fonts"
        if (Test-Path $windowsFonts) {
            $env:QT_QPA_FONTDIR = $windowsFonts
        }
    }
    $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
    $env:IBKR_BOT_HEADLESS_SIGNALS = "1"
    $root = Split-Path -Parent $PSScriptRoot
    Set-Location $root

    function Invoke-LoggedNative {
        param(
            [string]$Description,
            [string]$FilePath,
            [string[]]$Arguments,
            [string]$LogPath
        )
        Write-Host "==> $Description"
        Write-Host "    Logging detailed output to $LogPath"
        Remove-Item -Force $LogPath -ErrorAction SilentlyContinue
        & $FilePath @Arguments 2>&1 | Tee-Object -FilePath $LogPath
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            throw "$Description failed with exit code $exitCode. See $LogPath"
        }
    }

    if (!(Test-Path ".venv")) {
        py -3.11 -m venv .venv
    }
    $python = Join-Path $root ".venv\Scripts\python.exe"
    & $python -m pip install --upgrade pip
    & $python -m pip install -r requirements.txt
    & $python -m compileall -q app tests scripts main.py

    & $python -m coverage erase
    if ($LASTEXITCODE -ne 0) {
        throw "Could not erase the previous coverage data."
    }
    Invoke-LoggedNative "Run every pytest test with ResourceWarning and branch-coverage checks" $python @(
        "-X", "utf8",
        "-W", "error::ResourceWarning",
        "-m", "coverage", "run",
        "--branch", "--source=app,main",
        "-m", "pytest",
        "-q", "--tb=short", "-ra", "--disable-warnings"
    ) (Join-Path $root "run_tests_pytest.log")
    Invoke-LoggedNative "Report application statement and branch coverage" $python @(
        "-m", "coverage", "report", "--show-missing", "--fail-under=75"
    ) (Join-Path $root "run_tests_coverage.log")

    & $python -m coverage json -o coverage.json
    if ($LASTEXITCODE -ne 0) {
        throw "Could not write coverage.json."
    }
    & $python -m coverage xml -o coverage.xml
    if ($LASTEXITCODE -ne 0) {
        throw "Could not write coverage.xml."
    }
    Invoke-LoggedNative "Require entry coverage for every executable application callable" $python @(
        "scripts\check_callable_coverage.py", "--coverage-json", "coverage.json", "--source", "app", "--source", "main.py"
    ) (Join-Path $root "run_tests_callable_coverage.log")
    Invoke-LoggedNative "Run safety mutation smoke tests" $python @(
        "scripts\run_mutation_smoke.py"
    ) (Join-Path $root "run_tests_mutation_smoke.log")
    Invoke-LoggedNative "Run CSV simulation fixtures" $python @("scripts\run_all_simulations.py") (Join-Path $root "run_tests_simulations.log")
} finally {
    Restore-IbkrTestEnvironment
}

Wait-IbkrScriptPause
