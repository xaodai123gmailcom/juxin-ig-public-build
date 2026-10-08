# Runner-local, opt-in observation. Never used to decide whether a gate passes.
function New-PublicInstalledObserver {
    param([string]$Directory, [string]$RunNonce, [string]$InvocationId,
        [ValidateSet('nsis', 'native-smoke', 'recovery')][string]$Phase)
    if ([string]::IsNullOrEmpty($Directory)) { return $null }
    try {
        if ($RunNonce -cnotmatch '^[0-9a-f]{32}$' -or $InvocationId -cnotmatch '^[0-9a-f]{32}$' -or
            -not [IO.Path]::IsPathRooted($Directory) -or -not (Test-Path -LiteralPath $Directory -PathType Container)) {
            return $null
        }
        # The closure binds one parent invocation and one fixed call. Nothing is
        # inherited by child processes and no raw argv/environment is captured.
        $Observer = {
            param([string]$Event, [hashtable]$Observation)
            if ($Event -notin @('entered', 'bound', 'settled')) { return }
            $Value = @{ schema = 1; runNonce = $RunNonce; invocationId = $InvocationId
                phase = $Phase; event = $Event; observation = $Observation }
            $Bytes = [Text.Encoding]::UTF8.GetBytes(($Value | ConvertTo-Json -Depth 5 -Compress))
            if ($Bytes.Length -gt 16384) { return }
            $Stream = $null
            try {
                # Immutable per-event slots prevent late/duplicate calls from
                # silently replacing an earlier request in the same phase.
                $Stream = [IO.File]::Open((Join-Path $Directory ($Phase + '-' + $Event + '.json')),
                    [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
                $Stream.Write($Bytes, 0, $Bytes.Length)
            } finally { if ($null -ne $Stream) { $Stream.Dispose() } }
        }.GetNewClosure()
        & $Observer 'entered' @{} | Out-Null
        return $Observer
    } catch { return $null }
}
