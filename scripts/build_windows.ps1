param(
    [switch]$RunTests,
    [switch]$CleanVenv
)

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
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$version = "3.1.2"
$appName = "IBKRTradingBot"
$releaseName = "${appName}_${version}_Windows"
$releaseDirectory = Join-Path $root "release"
$releaseRoot = Join-Path $releaseDirectory $releaseName
$releaseZip = Join-Path $releaseDirectory "$releaseName.zip"
$checksumsPath = Join-Path $releaseDirectory "SHA256SUMS.txt"

function Invoke-Checked {
    param(
        [string]$Description,
        [scriptblock]$Command
    )
    Write-Host "==> $Description"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
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

if ($CleanVenv -and (Test-Path ".venv")) {
    Write-Host "Removing existing virtual environment..."
    Remove-Item -Recurse -Force ".venv"
}

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
$pyinstallerExe = Join-Path $root ".venv\Scripts\pyinstaller.exe"

Invoke-Checked "Upgrade pip" { & $python -m pip install --upgrade pip }
Invoke-Checked "Install requirements" { & $python -m pip install -r requirements.txt }

if ($RunTests) {
    # Optional pre-build gate 1: tests that do not require a live TWS/Gateway session.
    Invoke-Checked "Run pytest" { & $python -m pytest }

    # Optional pre-build gate 2: deterministic CSV simulation fixtures.
    # One runner executes all fixtures in a single interpreter to avoid repeated
    # process startup while preserving the same per-scenario checks.
    Invoke-Checked "Run CSV simulation fixtures" { & $python scripts\run_all_simulations.py }
} else {
    Write-Host "Skipping full tests for faster, more reliable packaging."
    Write-Host "Run .\scripts\run_tests.ps1 separately, or use .\scripts\build_windows.ps1 -RunTests."
}

foreach ($path in @("build", "dist", $releaseRoot, $releaseZip, $checksumsPath)) {
    if (Test-Path $path) {
        Remove-Item -Recurse -Force $path
    }
}
$buildLog = Join-Path $root "build_pyinstaller.log"
Remove-Item -Force $buildLog -ErrorAction SilentlyContinue

Write-Host "==> Build Windows executable with PyInstaller"

function Invoke-PyInstallerLogged {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$LogPath
    )

    # PyInstaller can write informational warnings to stderr, for example when the
    # terminal is elevated. With $ErrorActionPreference = Stop, direct native stderr
    # output can become a PowerShell NativeCommandError even when PyInstaller exits
    # successfully. Start-Process redirects stdout/stderr to files, then this script
    # decides success only from the process exit code and the produced .exe.
    $stdoutLog = "$LogPath.stdout"
    $stderrLog = "$LogPath.stderr"
    Remove-Item -Force $stdoutLog, $stderrLog -ErrorAction SilentlyContinue

    $process = Start-Process -FilePath $FilePath `
        -ArgumentList $Arguments `
        -NoNewWindow `
        -Wait `
        -PassThru `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog

    $combined = @()
    if (Test-Path $stdoutLog) { $combined += Get-Content $stdoutLog }
    if (Test-Path $stderrLog) { $combined += Get-Content $stderrLog }

    # Tee-Object writes its input to PowerShell's success-output stream. Consume
    # that stream with Out-Host so callers receive only the integer exit code,
    # rather than an array containing every PyInstaller log line plus the code.
    $combined | Tee-Object -FilePath $LogPath | Out-Host

    return [int]($process.ExitCode)
}

# Use the pyinstaller.exe entry point first. This is the validated Windows path.
# PyInstaller already sees the direct imports; forcing a broad dependency walk can
# make analysis substantially slower in some Python/PyInstaller environments.
$pyinstallerArgs = @("--clean", "--noconsole", "--onedir", "--name", $appName, "main.py")
if (Test-Path $pyinstallerExe) {
    $pyinstallerExitCode = Invoke-PyInstallerLogged -FilePath $pyinstallerExe -Arguments $pyinstallerArgs -LogPath $buildLog
} else {
    $pyinstallerExitCode = Invoke-PyInstallerLogged -FilePath $python -Arguments (@("-m", "PyInstaller") + $pyinstallerArgs) -LogPath $buildLog
}

if ($pyinstallerExitCode -ne 0) {
    throw "PyInstaller failed with exit code $pyinstallerExitCode. See $buildLog"
}

$exePath = Join-Path $root "dist\IBKRTradingBot\IBKRTradingBot.exe"
if (!(Test-Path $exePath)) {
    throw "PyInstaller completed but $exePath was not created. See $buildLog"
}

Write-Host "==> Assemble versioned release folder"
$guiTarget = Join-Path $releaseRoot "GUI"
New-Item -ItemType Directory -Path $guiTarget -Force | Out-Null

Copy-Item -Path (Join-Path $root "dist\$appName\*") -Destination $guiTarget -Recurse -Force
Copy-Item -Path (Join-Path $root "README.md") -Destination $releaseRoot -Force
Copy-Item -Path (Join-Path $root "CHANGELOG.md") -Destination $releaseRoot -Force
Copy-Item -Path (Join-Path $root "LICENSE") -Destination $releaseRoot -Force
Copy-Item -Path (Join-Path $root "SECURITY.md") -Destination $releaseRoot -Force
Copy-Item -Path (Join-Path $root "docs") -Destination $releaseRoot -Recurse -Force

$quickStart = @"
BouncyBot - IBKR Portable Trading Bot $version

Start the application:
  GUI\IBKRTradingBot.exe

Keep the complete GUI folder together. Do not copy only the executable.
The release folder must be writable because the SQLite database and generated
runtime folders are created beside the executable.

Use an IBKR paper account for initial validation before live trading.
"@
Set-Content -Path (Join-Path $releaseRoot "QUICK_START.txt") -Value $quickStart -Encoding UTF8

$releaseExePath = Join-Path $guiTarget "$appName.exe"
if (!(Test-Path $releaseExePath)) {
    throw "Release assembly completed but $releaseExePath was not created."
}

Write-Host "==> Create versioned release ZIP"
Compress-Archive -Path $releaseRoot -DestinationPath $releaseZip -CompressionLevel Optimal -Force
if (!(Test-Path $releaseZip)) {
    throw "Release ZIP was not created at $releaseZip."
}

$hashLines = @()
foreach ($file in @($releaseExePath, $releaseZip)) {
    $hash = Get-FileHash -Path $file -Algorithm SHA256
    $relative = $file.Substring($root.Length + 1)
    $hashLines += "$($hash.Hash.ToLowerInvariant())  $relative"
}
Set-Content -Path $checksumsPath -Value $hashLines -Encoding ASCII

Write-Host ""
Write-Host "Build complete:"
Write-Host "  $releaseRoot"
Write-Host "  $releaseZip"
Write-Host "  $checksumsPath"
Write-Host ""
Write-Host "Executable: $releaseExePath"
Write-Host "The SQLite file will be created beside IBKRTradingBot.exe when the app runs."

Wait-IbkrScriptPause
