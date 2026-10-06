$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$toolRoot = Split-Path -Parent $PSScriptRoot
function Test-CheckSource([string]$directory) {
    if (-not $directory) { return $false }
    foreach ($relative in @('package.json', 'node_modules\electron\dist\electron.exe', 'dist-electron\embedded-browser.js', 'dist-electron\account-permissions.js')) {
        if (-not (Test-Path -LiteralPath (Join-Path $directory $relative) -PathType Leaf)) { return $false }
    }
    try { return (Get-Content -LiteralPath (Join-Path $directory 'package.json') -Raw -Encoding UTF8 | ConvertFrom-Json).name -eq 'juxin-ig-audience-collector-newgen' } catch { return $false }
}
$source = $null
foreach ($candidate in @($toolRoot, (Split-Path -Parent $toolRoot))) {
    if (Test-CheckSource $candidate) { $source = $candidate; break }
}
while (-not $source) {
    $picker = New-Object System.Windows.Forms.FolderBrowserDialog
    $picker.Description = '选择已经成功构建过的聚鑫国际源码目录（里面有 START_HERE_NEWGEN.bat 和 node_modules）'
    $picker.ShowNewFolderButton = $false
    if ($picker.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { $picker.Dispose(); exit 2 }
    $selected = $picker.SelectedPath
    $picker.Dispose()
    if (Test-CheckSource $selected) { $source = $selected } else {
        [System.Windows.Forms.MessageBox]::Show('此目录没有已构建的浏览器环境。请选择上次成功生成安装包的源码目录，不是软件安装目录。', '请选择原构建目录') | Out-Null
    }
}
$report = Join-Path $toolRoot ('whatsapp-check-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')
$electron = Join-Path $source 'node_modules\electron\dist\electron.exe'
$script = Join-Path $PSScriptRoot 'whatsapp-comparison.cjs'
$info = New-Object System.Diagnostics.ProcessStartInfo
$info.FileName = $electron
$info.Arguments = '"' + $script + '"'
$info.WorkingDirectory = $source
$info.UseShellExecute = $false
$info.CreateNoWindow = $true
$info.RedirectStandardOutput = $true
$info.RedirectStandardError = $true
$info.EnvironmentVariables['JUXIN_WA_CHECK_SOURCE'] = $source
$info.EnvironmentVariables['JUXIN_WA_CHECK_OUTPUT'] = $report
$info.EnvironmentVariables.Remove('ELECTRON_RUN_AS_NODE')
$info.EnvironmentVariables.Remove('JUXIN_WA_CHECK_FIXTURE')
Write-Host '开始同内核检查，约 3 分钟。会依次出现 4 个临时 WhatsApp 窗口，请不要扫码。'
Write-Host '无需安装、更新内核或清除原账号资料。关闭测试窗口可以提前结束。'
$process = New-Object System.Diagnostics.Process
$process.StartInfo = $info
$null = $process.Start()
$stdout = $process.StandardOutput.ReadToEndAsync()
$stderr = $process.StandardError.ReadToEndAsync()
$process.WaitForExit()
$outText = $stdout.GetAwaiter().GetResult()
$errText = $stderr.GetAwaiter().GetResult()
if (Test-Path -LiteralPath $report) {
    Write-Host ('检查结果：' + $report)
    Write-Host '请只把 whatsapp-check-日期时间.json 发到对话中。'
} else {
    [ordered]@{tool='WhatsApp comparison launcher';completed=$false;exitCode=$process.ExitCode;error='检查程序未能启动';runtimeOutput=($outText + $errText).Substring(0,[Math]::Min(8000,($outText + $errText).Length))} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $report -Encoding UTF8
    Write-Host ('启动失败，原因已保存在：' + $report)
    Invoke-Item -LiteralPath (Split-Path -Parent $report)
}
exit $process.ExitCode
