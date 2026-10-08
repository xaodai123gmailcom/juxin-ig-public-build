# Thin adapter only. Windows APIs and containment live in owned_process.py.
# Python is the same declared prerequisite already required by install_windows.ps1.
function Get-IgacSupervisorPython {
    param([string]$PreferredExecutable = "")
    if (-not [string]::IsNullOrWhiteSpace($PreferredExecutable)) {
        if (-not (Test-Path -LiteralPath $PreferredExecutable -PathType Leaf)) { throw "Selected Python supervisor prerequisite was not found; no target was started" }
        return (Resolve-Path -LiteralPath $PreferredExecutable).Path
    }
    $Command = Get-Command python -CommandType Application -TotalCount 1 -ErrorAction SilentlyContinue
    if ($null -eq $Command -or -not (Test-Path -LiteralPath $Command.Source -PathType Leaf)) {
        throw "Owned process supervision requires the existing standard Windows x64 Python 3.11-3.14 prerequisite on PATH; no target was started."
    }
    return $Command.Source
}

function Test-IgacOwnedReceiptShape {
    param($Receipt)
    if ($null -eq $Receipt -or $Receipt -isnot [pscustomobject]) { return $false }
    foreach ($Name in @('schemaVersion','requestId','supervisorPid','launchTargetPid','requestedExecutable','launchExecutable',
        'targetExitCode','targetExitCodeUnsigned','outcome','budgetLabel','executionLimitSeconds','elapsedSeconds','confirmedTreeEmpty','errors')) {
        if ($Receipt.PSObject.Properties.Name -cnotcontains $Name) { return $false }
    }
    $IsInteger = { param($Value) ($Value -is [int]) -or ($Value -is [long]) }
    $IsFiniteNumber = { param($Value)
        (($Value -is [int]) -or ($Value -is [long]) -or ($Value -is [double]) -or ($Value -is [decimal])) -and
        -not [double]::IsNaN([double]$Value) -and -not [double]::IsInfinity([double]$Value)
    }
    if ($null -eq $Receipt -or $Receipt -isnot [pscustomobject] -or -not (& $IsInteger $Receipt.schemaVersion) -or $Receipt.schemaVersion -ne 1 -or
        $Receipt.requestId -isnot [string] -or $Receipt.requestId -cnotmatch '^[0-9a-f]{32}$' -or
        -not (& $IsInteger $Receipt.supervisorPid) -or $Receipt.supervisorPid -le 0 -or
        ($null -ne $Receipt.launchTargetPid -and (-not (& $IsInteger $Receipt.launchTargetPid) -or $Receipt.launchTargetPid -le 0)) -or
        ($null -ne $Receipt.targetExitCode -and (-not (& $IsInteger $Receipt.targetExitCode) -or $Receipt.targetExitCode -lt -2147483648 -or $Receipt.targetExitCode -gt 2147483647)) -or
        $Receipt.confirmedTreeEmpty -isnot [bool] -or $Receipt.errors -isnot [array] -or
        $Receipt.outcome -notin @('completed', 'target-exited-nonzero', 'execution-timeout', 'cancelled', 'cancelled-before-launch', 'descendant-drain-timeout', 'supervision-error', 'log-size-limit') -or
        $Receipt.requestedExecutable -isnot [string] -or [string]::IsNullOrEmpty($Receipt.requestedExecutable) -or
        ($null -ne $Receipt.launchExecutable -and $Receipt.launchExecutable -isnot [string]) -or
        $Receipt.budgetLabel -isnot [string] -or [string]::IsNullOrEmpty($Receipt.budgetLabel) -or
        -not (& $IsFiniteNumber $Receipt.executionLimitSeconds) -or $Receipt.executionLimitSeconds -le 0 -or
        -not (& $IsFiniteNumber $Receipt.elapsedSeconds) -or $Receipt.elapsedSeconds -lt 0) { return $false }
    if ($null -eq $Receipt.targetExitCode) {
        if ($null -ne $Receipt.targetExitCodeUnsigned) { return $false }
    } else {
        if (-not (& $IsInteger $Receipt.targetExitCodeUnsigned) -or $Receipt.targetExitCodeUnsigned -lt 0 -or $Receipt.targetExitCodeUnsigned -gt 4294967295) { return $false }
        $ExpectedUnsigned = [long]$Receipt.targetExitCode
        if ($ExpectedUnsigned -lt 0) { $ExpectedUnsigned += 4294967296 }
        if ($Receipt.targetExitCodeUnsigned -ne $ExpectedUnsigned) { return $false }
    }
    foreach ($Failure in $Receipt.errors) { if ($Failure -isnot [string]) { return $false } }
    return $true
}

function Test-IgacOwnedCleanupReceipt {
    param($Receipt)
    return (Test-IgacOwnedReceiptShape $Receipt) -and $Receipt.confirmedTreeEmpty -and
        $Receipt.errors.Count -eq 0 -and $null -ne $Receipt.launchTargetPid -and $Receipt.supervisorPid -ne $Receipt.launchTargetPid -and -not [string]::IsNullOrEmpty($Receipt.launchExecutable)
}

function Test-IgacOwnedTerminalReceipt {
    param($Receipt)
    return (Test-IgacOwnedCleanupReceipt $Receipt) -and $null -ne $Receipt.targetExitCode -and
        (($Receipt.outcome -eq 'completed' -and $Receipt.targetExitCode -eq 0) -or
         ($Receipt.outcome -eq 'target-exited-nonzero' -and $Receipt.targetExitCode -ne 0))
}

function Test-IgacOwnedReceiptMatches {
    param($Receipt, [hashtable]$Request)
    return (Test-IgacOwnedReceiptShape $Receipt) -and $Receipt.requestId -ceq $Request.requestId -and
        [string]::Equals([string]$Receipt.requestedExecutable, [string]$Request.executable, [StringComparison]::OrdinalIgnoreCase) -and
        $Receipt.budgetLabel -ceq $Request.budgetLabel -and $Receipt.executionLimitSeconds -eq $Request.timeoutSeconds -and
        $Receipt.supervisorPid -ne $Receipt.launchTargetPid
}

function Read-IgacOwnedLog {
    param([string]$Path, [int]$TimeoutMilliseconds = 10000, [long]$MaximumBytes = 268435456)
    # Async FileStream read, bounded before Result. No child output pipe is used.
    $Stream = $null
    $Reader = $null
    $Task = $null
    try {
        $Stream = New-Object IO.FileStream($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete), 4096, $true)
        if ($Stream.Length -gt $MaximumBytes) { throw "Owned output log exceeds the bounded read limit: $Path" }
        $Reader = New-Object IO.StreamReader($Stream, [Text.Encoding]::UTF8, $true)
        $Task = $Reader.ReadToEndAsync()
        if (-not $Task.Wait($TimeoutMilliseconds)) { throw "Owned output read exceeded its deadline; original log retained: $Path" }
        return $Task.GetAwaiter().GetResult()
    } finally {
        # A stalled async operation must not become an unbounded synchronous Dispose.
        if ($null -eq $Task -or $Task.IsCompleted) {
            if ($null -ne $Reader) { $Reader.Dispose() } elseif ($null -ne $Stream) { $Stream.Dispose() }
        }
    }
}

function Invoke-IgacOwnedProcess {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][hashtable]$Request, [switch]$StreamOutput, [string]$SupervisorPython = "",
        [scriptblock]$DiagnosticObserver = $null)
    $Python = Get-IgacSupervisorPython -PreferredExecutable $SupervisorPython
    $Helper = Join-Path $PSScriptRoot "owned_process.py"
    if (-not (Test-Path -LiteralPath $Helper -PathType Leaf)) { throw "Owned process helper is missing; no target was started" }
    $Request.controlInput = $true
    $Request.requestId = [Guid]::NewGuid().ToString("N")
    $ReceiptPath = [IO.Path]::GetFullPath([string]$Request.stdoutPath) + ".owned-" + [Guid]::NewGuid().ToString("N") + ".json"
    $Request.receiptPath = $ReceiptPath
    # Optional CI observation receives this exact invocation, never a directory search.
    # It is not part of acceptance and cannot replace a receipt or original error.
    if ($null -ne $DiagnosticObserver) {
        try { & $DiagnosticObserver 'bound' @{
            requestId = $Request.requestId; receiptPath = $ReceiptPath
            executable = [string]$Request.executable; timeoutSeconds = $Request.timeoutSeconds
            budgetLabel = [string]$Request.budgetLabel
        } | Out-Null } catch { }
    }
    $Json = $Request | ConvertTo-Json -Depth 8 -Compress
    $Encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Json))
    $Process = New-Object System.Diagnostics.Process
    $Process.StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $Process.StartInfo.FileName = $Python
    # Only a fixed script path and base64 data cross the native argv boundary.
    if ($Helper.Contains('"')) { throw "Owned process helper path contains an invalid quote" }
    $Process.StartInfo.Arguments = '-I -X utf8 "' + $Helper + '" --request-base64 ' + $Encoded
    if ($Process.StartInfo.Arguments.Length -ge 30000) { throw "Owned process request exceeds the Windows argv budget; no target was started" }
    $Process.StartInfo.UseShellExecute = $false
    $Process.StartInfo.CreateNoWindow = $true
    $Process.StartInfo.RedirectStandardInput = $true
    # Target logs go directly to files. Do not create stdout/stderr pipe-EOF waits.
    $Process.StartInfo.RedirectStandardOutput = $false
    $Process.StartInfo.RedirectStandardError = $false
    $Process.StartInfo.WorkingDirectory = [string]$Request.workingDirectory
    $Started = $false
    $ProgressStream = $null
    $ProgressReader = $null
    $ProgressTask = $null
    $PrimaryFailure = $null
    $DiagnosticStage = 'supervisor-start'
    $OuterDeadlineExceeded = $false
    $CleanupWaitTimedOut = $false
    try {
        $Started = $Process.Start()
        if (-not $Started) { throw "Owned process supervisor did not start; no target receipt exists" }
        $LimitMilliseconds = [int][Math]::Ceiling(([double]$Request.timeoutSeconds + [double]$Request.drainSeconds + [double]$Request.terminationSeconds + 30) * 1000)
        $DiagnosticStage = 'supervisor-wait'
        $Watch = [Diagnostics.Stopwatch]::StartNew()
        $ProgressBuffer = New-Object char[] 16384
        while (-not $Process.WaitForExit(250)) {
            if ($Watch.ElapsedMilliseconds -ge $LimitMilliseconds) {
                $OuterDeadlineExceeded = $true
                $Process.StandardInput.Close()
                if (-not $Process.WaitForExit([int](([double]$Request.terminationSeconds + 5) * 1000))) {
                    throw "Owned supervisor exceeded its outer deadline; tree cleanup is unconfirmed; logs: $($Request.stdoutPath)"
                }
                throw "Owned supervisor exceeded its outer deadline; logs retained: $($Request.stdoutPath)"
            }
            if ($StreamOutput) {
                if ($null -eq $ProgressReader -and (Test-Path -LiteralPath $Request.stdoutPath -PathType Leaf)) {
                    # Local-file open and console host writes remain OS-I/O limits;
                    # target supervision runs independently in the Python owner.
                    $DiagnosticStage = 'progress-log-open'
                    $ProgressStream = New-Object IO.FileStream($Request.stdoutPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete), 4096, $true)
                    $ProgressReader = New-Object IO.StreamReader($ProgressStream, [Text.Encoding]::UTF8, $true)
                }
                if ($null -ne $ProgressReader) {
                    $DiagnosticStage = 'progress-log-read'
                    $ProgressTask = $ProgressReader.ReadAsync($ProgressBuffer, 0, $ProgressBuffer.Length)
                    if (-not $ProgressTask.Wait(2000)) { throw "Owned live output read exceeded its deadline; full log retained: $($Request.stdoutPath)" }
                    $Count = $ProgressTask.GetAwaiter().GetResult()
                    if ($Count -gt 0) { Write-Host -ErrorAction Stop -NoNewline (-join $ProgressBuffer[0..($Count - 1)]) }
                }
            }
            $DiagnosticStage = 'supervisor-wait'
        }
        if ($StreamOutput) {
            $DiagnosticStage = 'final-log-read'
            if ($null -ne $ProgressReader) {
                $ProgressTask = $ProgressReader.ReadToEndAsync()
                if (-not $ProgressTask.Wait(10000)) { throw "Owned final output read exceeded its deadline; full log retained: $($Request.stdoutPath)" }
                $Remainder = $ProgressTask.GetAwaiter().GetResult()
            } else {
                $Remainder = Read-IgacOwnedLog -Path $Request.stdoutPath
            }
            if (-not [string]::IsNullOrEmpty($Remainder)) { Write-Host -ErrorAction Stop -NoNewline $Remainder }
        }
        $DiagnosticStage = 'receipt-read'
        if (-not (Test-Path -LiteralPath $ReceiptPath -PathType Leaf)) {
            throw "Owned supervisor exited without a complete receipt (exit $($Process.ExitCode)); cleanup is unconfirmed; logs: $($Request.stdoutPath)"
        }
        $Receipt = (Read-IgacOwnedLog -Path $ReceiptPath -MaximumBytes 65536) | ConvertFrom-Json
        $DiagnosticStage = 'receipt-validation'
        $MatchesRequest = Test-IgacOwnedReceiptMatches $Receipt $Request
        $CleanupConfirmed = $MatchesRequest -and (Test-IgacOwnedCleanupReceipt $Receipt)
        if (-not $MatchesRequest -or -not (Test-IgacOwnedTerminalReceipt $Receipt) -or $Process.ExitCode -ne 0) {
            throw "Owned process failed: $($Receipt.outcome); cleanup confirmed=$CleanupConfirmed; errors=$($Receipt.errors -join '; '); logs: $($Request.stdoutPath); receipt: $ReceiptPath"
        }
        $DiagnosticStage = 'returned'
        return $Receipt
    } catch {
        $PrimaryFailure = $_
        throw
    } finally {
        if ($Started) {
            try { $Process.StandardInput.Close() } catch { }
            # EOF cancellation has its own bound even after an output error.
            try {
                if (-not $Process.HasExited -and -not $Process.WaitForExit([int](([double]$Request.terminationSeconds + 5) * 1000))) {
                    $CleanupWaitTimedOut = $true
                    Write-Warning "Owned cleanup remains unconfirmed; retain the original log and receipt paths"
                }
            } catch { Write-Warning "Owned supervisor exit could not be observed during cleanup" }
            # Never call parameterless WaitForExit, Task.Result, or PID-tree kill.
        }
        if ($null -eq $ProgressTask -or $ProgressTask.IsCompleted) {
            if ($null -ne $ProgressReader) { $ProgressReader.Dispose() } elseif ($null -ne $ProgressStream) { $ProgressStream.Dispose() }
        }
        # Observe only after the unchanged EOF/cleanup wait, including late receipts.
        if ($null -ne $DiagnosticObserver) {
            try {
                $SupervisorExitCode = $null
                if ($Started -and $Process.HasExited) { $SupervisorExitCode = [int]$Process.ExitCode }
                & $DiagnosticObserver 'settled' @{
                    requestId = $Request.requestId; adapterStage = $DiagnosticStage
                    supervisorStarted = [bool]$Started; supervisorExitCode = $SupervisorExitCode
                    outerDeadlineExceeded = [bool]$OuterDeadlineExceeded
                    cleanupWaitTimedOut = [bool]$CleanupWaitTimedOut
                } | Out-Null
            } catch { }
        }
        $Process.Dispose()
    }
}
