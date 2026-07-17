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
$script = Join-Path $PSScriptRoot "build_windows.ps1"
$__IbkrPreviousNoPause = [Environment]::GetEnvironmentVariable("IBKR_BOT_NO_PAUSE", "Process")
try {
    [Environment]::SetEnvironmentVariable("IBKR_BOT_NO_PAUSE", "1", "Process")
    & $script -RunTests
} finally {
    [Environment]::SetEnvironmentVariable("IBKR_BOT_NO_PAUSE", $__IbkrPreviousNoPause, "Process")
}

Wait-IbkrScriptPause
