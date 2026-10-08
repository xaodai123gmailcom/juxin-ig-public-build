. "$PSScriptRoot\owned_process.ps1"

function Invoke-IgacNativeCommandWithLog {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$FilePath,
        [AllowEmptyCollection()][string[]]$ArgumentList = @(),
        [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$LogPath,
        # Previously unbounded. This explicit orchestration cap does not replace
        # or extend any narrower test, stage, embedded-suite or build deadline.
        [ValidateRange(1, 86400)][int]$TimeoutSeconds = 14400,
        [scriptblock]$DiagnosticObserver = $null
    )
    if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) { throw "Native command was not found: $FilePath" }
    $FullLogPath = [IO.Path]::GetFullPath($LogPath)
    $Directory = Split-Path -Parent $FullLogPath
    if (-not [string]::IsNullOrWhiteSpace($Directory)) { New-Item -ItemType Directory -Path $Directory -Force | Out-Null }
    $global:LASTEXITCODE = $null
    $SupervisorPython = ""
    # Environment repair already selected its real base interpreter. Reuse that
    # exact Python target for supervision rather than holding an activated venv
    # redirector open while ensure_python_environment renames the old venv.
    if ([IO.Path]::GetFileName($FilePath).Equals("python.exe", [StringComparison]::OrdinalIgnoreCase)) {
        $SupervisorPython = (Resolve-Path -LiteralPath $FilePath).Path
    }
    $Receipt = Invoke-IgacOwnedProcess -SupervisorPython $SupervisorPython -StreamOutput -DiagnosticObserver $DiagnosticObserver -Request @{
        executable = (Resolve-Path -LiteralPath $FilePath).Path
        arguments = @($ArgumentList)
        workingDirectory = (Get-Location).ProviderPath
        stdoutPath = $FullLogPath
        stderrPath = $FullLogPath
        timeoutSeconds = $TimeoutSeconds
        budgetLabel = "Native command orchestration cap (narrower child deadlines unchanged)"
        drainSeconds = 10
        terminationSeconds = 10
    }
    $NativeExitCode = [int]$Receipt.targetExitCode
    $global:LASTEXITCODE = $NativeExitCode
    # The adapter streamed bounded chunks and the final file suffix. The full
    # log remains owned and does not depend on inherited output-pipe EOF.
    return $NativeExitCode
}
