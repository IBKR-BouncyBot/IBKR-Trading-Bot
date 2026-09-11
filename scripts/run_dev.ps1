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
$envNamesToRestore = @(
    "PYTHONUTF8",
    "PYTHONIOENCODING",
    "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE",
    "QT_QPA_PLATFORM",
    "QT_QPA_FONTDIR",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
    "IBKR_BOT_HEADLESS_SIGNALS"
)
$savedEnv = @{}
foreach ($name in $envNamesToRestore) {
    $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

function Restore-ProcessEnvironment {
    foreach ($name in $envNamesToRestore) {
        $value = $savedEnv[$name]
        if ($null -eq $value) {
            [Environment]::SetEnvironmentVariable($name, $null, "Process")
        } else {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

function Resolve-PythonLauncher {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try {
            & py -3.11 --version > $null 2>&1
            if ($LASTEXITCODE -eq 0) { return @("py", "-3.11") }
        } catch {}
        try {
            & py -3 --version > $null 2>&1
            if ($LASTEXITCODE -eq 0) { return @("py", "-3") }
        } catch {}
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @("python")
    }
    throw "Python was not found. Install Python 3.11+ and retry."
}

try {
    # Development launch must use the interactive Windows Qt platform. Clear
    # test-only offscreen/headless variables that may remain in the parent
    # PowerShell session before starting the operator GUI.
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONUNBUFFERED = "1"
    Remove-Item Env:\PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
    if ($env:OS -eq "Windows_NT") {
        $env:QT_QPA_PLATFORM = "windows"
        if ($env:WINDIR) {
            $windowsFonts = Join-Path $env:WINDIR "Fonts"
            if (Test-Path $windowsFonts) {
                $env:QT_QPA_FONTDIR = $windowsFonts
            }
        }
    } else {
        Remove-Item Env:\QT_QPA_PLATFORM -ErrorAction SilentlyContinue
        Remove-Item Env:\QT_QPA_FONTDIR -ErrorAction SilentlyContinue
    }
    Remove-Item Env:\PYTEST_DISABLE_PLUGIN_AUTOLOAD -ErrorAction SilentlyContinue
    Remove-Item Env:\IBKR_BOT_HEADLESS_SIGNALS -ErrorAction SilentlyContinue

    $root = Split-Path -Parent $PSScriptRoot
    Set-Location $root

    if (!(Test-Path ".venv\Scripts\python.exe")) {
        $launcher = Resolve-PythonLauncher
        Write-Host "Creating Python virtual environment with: $($launcher -join ' ')"
        if ($launcher.Length -gt 1) {
            & $launcher[0] $launcher[1] -m venv .venv
        } else {
            & $launcher[0] -m venv .venv
        }
        if ($LASTEXITCODE -ne 0 -or !(Test-Path ".venv\Scripts\python.exe")) {
            throw "Virtual environment was not created. Check Python installation."
        }
    }

    $python = Join-Path $root ".venv\Scripts\python.exe"
    & $python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed with exit code $LASTEXITCODE" }

    & $python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "requirements install failed with exit code $LASTEXITCODE" }

    & $python main.py
    if ($LASTEXITCODE -ne 0) { throw "Application exited with code $LASTEXITCODE" }
} finally {
    Restore-ProcessEnvironment
}

Wait-IbkrScriptPause
