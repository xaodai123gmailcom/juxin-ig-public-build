$script:IgacBuildMutexName = "Local\JuxinIGAudienceCollector-SetupBuild-v035"

function Enter-IgacBuildMutex {
    $Mutex = New-Object System.Threading.Mutex -ArgumentList @(
        $false,
        $script:IgacBuildMutexName
    )
    $Acquired = $false
    try {
        $Acquired = $Mutex.WaitOne(0, $false)
    } catch [System.Threading.AbandonedMutexException] {
        # WaitOne grants ownership when the previous process died without
        # releasing the mutex.  The interrupted runtime marker was already
        # fail-closed and this new run may safely repair it.
        $Acquired = $true
    }
    if (-not $Acquired) {
        $Mutex.Dispose()
        throw "Another Juxin IG setup or build is already running. Keep that window open and do not start a second copy."
    }
    return $Mutex
}

function Exit-IgacBuildMutex([System.Threading.Mutex]$Mutex) {
    if ($null -eq $Mutex) {
        return
    }
    try {
        $Mutex.ReleaseMutex()
    } finally {
        $Mutex.Dispose()
    }
}
