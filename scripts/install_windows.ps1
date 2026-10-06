$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
. "$PSScriptRoot\build_mutex.ps1"
. "$PSScriptRoot\invoke_native_logged.ps1"
$IgacBuildMutex = Enter-IgacBuildMutex

try {
$RuntimeReadyMarker = Join-Path $ProjectRoot ".venv\igac-runtime-ready.json"

# ensure_python_environment.py invalidates the old marker after checking that
# .venv is an ordinary project directory. Only step 8 can seal readiness.

function Assert-LastExitCode([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed (exit code: $LASTEXITCODE)"
    }
}

if (-not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess) {
    throw "64-bit Windows PowerShell is required. Run this file from C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe."
}
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python was not found. Install Python and enable Add Python to PATH."
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "Node.js was not found. Install Node.js LTS and reopen this file."
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "npm was not found. Reinstall Node.js LTS with the default npm component."
}

python -I -X utf8 -c "import platform, struct, sys, sysconfig; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 15) and platform.python_implementation() == 'CPython' and struct.calcsize('P') == 8 and platform.machine().casefold() in {'amd64', 'x86_64'} and not sysconfig.get_config_var('Py_GIL_DISABLED') else 1)"
if ($LASTEXITCODE -ne 0) {
    $DetectedPython = python -I -X utf8 -c "import platform, struct, sys, sysconfig; print('version=%s; implementation=%s; machine=%s; pointer_bits=%s; free_threaded=%s; executable=%s' % (sys.version.split()[0], platform.python_implementation(), platform.machine(), struct.calcsize('P') * 8, bool(sysconfig.get_config_var('Py_GIL_DISABLED')), sys.executable))"
    throw "Python compatibility check failed: standard 64-bit CPython 3.11-3.14 for Windows x64 is required; ARM64, 32-bit, and free-threaded builds are unsupported. Detected: $DetectedPython"
}
node -e "const major = Number(process.versions.node.split('.')[0]); process.exit(major >= 22 && process.platform === 'win32' && process.arch === 'x64' ? 0 : 1)"
if ($LASTEXITCODE -ne 0) {
    $DetectedNode = node -p "'version=' + process.version + '; platform=' + process.platform + '; arch=' + process.arch + '; executable=' + process.execPath"
    throw "Node.js compatibility check failed: Node.js 22 or newer for Windows x64 is required; 32-bit and ARM64 builds are unsupported. Detected: $DetectedNode"
}

# Renderer regressions import the project's real .ts modules directly.
# The old major-only check admitted Node 22 releases without this capability.
node --input-type=module -e "import('./renderer/src/collection-task-rows.ts').catch(() => process.exit(1))"
if ($LASTEXITCODE -ne 0) {
    throw "Node.js cannot load the project's TypeScript modules. Use Windows x64 Node.js 22.18+ or 24+ with type stripping enabled, then reopen this build."
}

# Resolve the base interpreter before starting repair. An activated .venv must
# not hold its own python.exe open while a damaged environment is backed up.
# JSON is ASCII here, preserving Unicode paths across Windows PowerShell 5.1.
$SelectedPython = (Get-Command python -CommandType Application -TotalCount 1 -ErrorAction Stop).Source
$BasePythonJson = & $SelectedPython -I -X utf8 -c "import json, sys; print(json.dumps(getattr(sys, '_base_executable', sys.executable)))"
Assert-LastExitCode "Locate base Python interpreter"
$BasePython = [string]($BasePythonJson | ConvertFrom-Json)
if (-not (Test-Path -LiteralPath $BasePython -PathType Leaf)) {
    throw "Base Python interpreter was not found; repair the standard CPython installation."
}
$PythonEnvironmentLog = Join-Path $ProjectRoot "installer-output\python-environment-full.log"
$PipUpgradeLog = Join-Path $ProjectRoot "installer-output\pip-upgrade-full.log"
$PythonDependenciesLog = Join-Path $ProjectRoot "installer-output\python-dependencies-full.log"

Write-Host "[1/8] Checking and repairing the Python environment..." -ForegroundColor Cyan
$EnvironmentExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath $BasePython `
    -ArgumentList @("-I", "-X", "utf8", "scripts\ensure_python_environment.py", "--project-root", $ProjectRoot) `
    -LogPath $PythonEnvironmentLog
if ($EnvironmentExitCode -ne 0) {
    throw "Python environment repair failed (exit code: $EnvironmentExitCode). See installer-output\python-environment-full.log."
}
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

Write-Host "[2/8] Updating pip..." -ForegroundColor Cyan
$PipUpgradeExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath $VenvPython `
    -ArgumentList @("-I", "-X", "utf8", "scripts\upgrade_build_pip.py", "--project-root", $ProjectRoot) `
    -LogPath $PipUpgradeLog
if ($PipUpgradeExitCode -ne 0) {
    throw "Pip update or local verification failed (exit code: $PipUpgradeExitCode). See installer-output\pip-upgrade-full.log."
}

Write-Host "[3/8] Installing Python dependencies..." -ForegroundColor Cyan
$PythonDependenciesExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath $VenvPython `
    -ArgumentList @("-I", "-X", "utf8", "scripts\install_python_dependencies.py", "--project-root", $ProjectRoot) `
    -LogPath $PythonDependenciesLog
if ($PythonDependenciesExitCode -ne 0) {
    throw "Install Python dependencies failed (exit code: $PythonDependenciesExitCode). See installer-output\python-dependencies-full.log."
}

# Check the exact build interpreter before models, npm or native compilation.
$TimezoneDataExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath $VenvPython `
    -ArgumentList @("-I", "-X", "utf8", "scripts\verify_timezone_data.py") `
    -LogPath (Join-Path $ProjectRoot "installer-output\timezone-data-full.log")
if ($TimezoneDataExitCode -ne 0) {
    throw "Verify packaged timezone data failed (exit code: $TimezoneDataExitCode). See installer-output\timezone-data-full.log."
}

Write-Host "[4/8] Checking Microsoft Visual C++ runtime..." -ForegroundColor Cyan
& "$PSScriptRoot\ensure_microsoft_vc_runtime.ps1"

Write-Host "[5/8] Disabling OpenVINO telemetry for local-only recognition..." -ForegroundColor Cyan
if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    throw "Disable OpenVINO telemetry failed: LOCALAPPDATA is unavailable"
}
$OpenVinoTelemetryDirectory = Join-Path $env:LOCALAPPDATA "Intel Corporation"
$OpenVinoTelemetryConsentFile = Join-Path $OpenVinoTelemetryDirectory "openvino_telemetry"
New-Item -ItemType Directory -Path $OpenVinoTelemetryDirectory -Force | Out-Null
[System.IO.File]::WriteAllText($OpenVinoTelemetryConsentFile, "0", [System.Text.Encoding]::ASCII)
if (-not (Test-Path -LiteralPath $OpenVinoTelemetryConsentFile -PathType Leaf) -or
    [System.IO.File]::ReadAllText($OpenVinoTelemetryConsentFile) -ne "0") {
    throw "Disable OpenVINO telemetry failed: consent setting could not be verified"
}

Write-Host "[6/8] Downloading and verifying local person-recognition models..." -ForegroundColor Cyan
& .venv\Scripts\python.exe scripts\download_person_models.py
Assert-LastExitCode "Prepare local person-recognition models"

Write-Host "[7/8] Installing desktop APP dependencies..." -ForegroundColor Cyan
if (Test-Path "package-lock.json") {
    npm ci --include=dev --include=optional --ignore-scripts=false --bin-links=true
} else {
    npm install --include=dev --include=optional --ignore-scripts=false --bin-links=true
}
Assert-LastExitCode "Install desktop APP dependencies"
npm ls --depth=0 --silent
Assert-LastExitCode "Validate desktop APP dependency consistency"

Write-Host "[8/8] Running real local inference and sealing runtime readiness..." -ForegroundColor Cyan
$NativeManifest = Join-Path $ProjectRoot "build\openvino-native-manifest.json"
& .venv\Scripts\python.exe scripts\verify_openvino_windows.py source --manifest $NativeManifest
Assert-LastExitCode "Verify installed OpenVINO native runtime and real CPU inference"
& .venv\Scripts\python.exe scripts\verify_runtime_ready.py write --marker $RuntimeReadyMarker
Assert-LastExitCode "Write runtime readiness marker"
& .venv\Scripts\python.exe scripts\verify_runtime_ready.py check --marker $RuntimeReadyMarker
Assert-LastExitCode "Verify runtime readiness marker"

Write-Host "Runtime installation completed." -ForegroundColor Green
} finally {
    Exit-IgacBuildMutex $IgacBuildMutex
}
