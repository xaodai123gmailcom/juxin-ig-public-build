$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
. "$PSScriptRoot\build_mutex.ps1"
$IgacBuildMutex = Enter-IgacBuildMutex

function Assert-LastExitCode([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed (exit code: $LASTEXITCODE)"
    }
}

try {
    $RuntimeReady = (
        (Test-Path ".venv\Scripts\python.exe" -PathType Leaf) -and
        (Test-Path "node_modules" -PathType Container) -and
        (Test-Path ".venv\igac-runtime-ready.json" -PathType Leaf)
    )
    if ($RuntimeReady) {
        & node -e "const major=Number(process.versions.node.split('.')[0]); process.exit(major>=22 && process.platform==='win32' && process.arch==='x64' ? 0 : 1)"
        $RuntimeReady = $LASTEXITCODE -eq 0
    }
    if ($RuntimeReady) {
        & .venv\Scripts\python.exe scripts\verify_runtime_ready.py check --marker .venv\igac-runtime-ready.json
        $RuntimeReady = $LASTEXITCODE -eq 0
    }
    if ($RuntimeReady) {
        & npm ls --depth=0 --silent
        $RuntimeReady = $LASTEXITCODE -eq 0
    }

    if (-not $RuntimeReady) {
        Write-Host "Runtime is missing, incomplete, or stale. Starting automatic repair..." -ForegroundColor Yellow
        & "$PSScriptRoot\install_windows.ps1"
    }

    # Recheck all three boundaries after a repair.  The install script holds a
    # recursive acquisition of this same mutex, so no second setup/build can
    # modify the environment between these checks and application startup.
    & node -e "const major=Number(process.versions.node.split('.')[0]); process.exit(major>=22 && process.platform==='win32' && process.arch==='x64' ? 0 : 1)"
    Assert-LastExitCode "Verify source APP Node runtime"
    & .venv\Scripts\python.exe scripts\verify_runtime_ready.py check --marker .venv\igac-runtime-ready.json
    Assert-LastExitCode "Verify source APP Python runtime"
    & npm ls --depth=0 --silent
    Assert-LastExitCode "Verify source APP desktop dependencies"

    $env:PYTHON_EXECUTABLE = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    Write-Host "Starting the APP. Keep this window open..." -ForegroundColor Cyan
    & npm run dev
    Assert-LastExitCode "Run source desktop APP"
} finally {
    Exit-IgacBuildMutex $IgacBuildMutex
}
