# Shared by the source launcher, test runner and Windows build. This helper
# never removes an environment or touches the application's database.
$script:IbkrRuntimeCheck = Join-Path $PSScriptRoot "check_python_runtime.py"

function Resolve-PythonLauncher {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try {
            & py -3.14 $script:IbkrRuntimeCheck > $null 2>&1
            if ($LASTEXITCODE -eq 0) { return @("py", "-3.14") }
        } catch {}
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        try {
            & python $script:IbkrRuntimeCheck > $null 2>&1
            if ($LASTEXITCODE -eq 0) { return @("python") }
        } catch {}
    }
    throw "Install standard CPython 3.14.x (with the Windows Python launcher) and retry. Python 3.14t and other branches are not supported."
}

function Assert-IbkrPythonRuntime {
    param([string]$Python)
    & $Python $script:IbkrRuntimeCheck
    if ($LASTEXITCODE -ne 0) {
        throw "The existing .venv is incompatible. Close BouncyBot, rename .venv to .venv_previous, and rerun this script to create a Python 3.14 environment. Keep the database and other application files in place."
    }
}
