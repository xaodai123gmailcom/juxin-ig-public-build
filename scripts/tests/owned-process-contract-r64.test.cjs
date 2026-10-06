/* Static adapter/protection contracts supplement API fakes; they are not PowerShell execution. */
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const root=path.resolve(__dirname,'../..');
const read=file=>fs.readFileSync(path.join(root,file),'utf8').replace(/^\uFEFF/,'');

test('one Python native implementation is shared by the PS and Node adapters',()=>{
 const ps=read('scripts/owned_process.ps1'),node=read('scripts/owned_process.cjs');
 assert.match(ps,/owned_process\.py/);assert.match(node,/owned_process\.py/);
 assert.doesNotMatch(ps,/Add-Type|DllImport|CreateJobObject/);
 assert.doesNotMatch(node,/taskkill\.exe.*\/PID|execSync|shell:\s*true/);
 for(const text of [ps,node,read('scripts/owned_process.py')])assert.doesNotMatch(text,/Set-ExecutionPolicy|Unblock-File|ExecutionPolicy Bypass|Get-Process|Win32_Process/);
});

test('native logging preserves selected base Python during venv repair and explicit cap',()=>{
 const log=read('scripts/invoke_native_logged.ps1'),install=read('scripts/install_windows.ps1');
 assert.match(log,/\$TimeoutSeconds = 14400/);
 assert.match(log,/GetFileName\(\$FilePath\)\.Equals\("python\.exe"/);
 assert.match(log,/-SupervisorPython \$SupervisorPython -StreamOutput/);
 assert.match(install,/\$EnvironmentExitCode = Invoke-IgacNativeCommandWithLog\s+`\s+-FilePath \$BasePython/);
 assert.match(log,/\$global:LASTEXITCODE = \$NativeExitCode/);
});

test('PS waits and reads never use unbounded process wait or unfinished task Result',()=>{
 const ps=read('scripts/owned_process.ps1'),frozen=read('scripts/test_frozen_openvino.ps1');
 for(const source of [ps,frozen]){const text=source.replace(/^\s*#.*$/gm,'');assert.doesNotMatch(text,/\.WaitForExit\(\s*\)|\.Result\b/);assert.doesNotMatch(text,/taskkill\.exe|Get-Process/)}
 assert.match(ps,/ReadAsync\(\$ProgressBuffer/);assert.match(ps,/ProgressTask\.Wait\(2000\)/);
 assert.match(ps,/ProgressTask\.Wait\(10000\)/);assert.match(ps,/Test-IgacOwnedReceiptMatches \$Receipt \$Request/);
 assert.match(ps,/\$CleanupConfirmed = \$MatchesRequest -and/);
});

test('frozen smoke retains original execution deadline, clean environment and every proof marker',()=>{
 const text=read('scripts/test_frozen_openvino.ps1');
 assert.match(text,/\$TimeoutSeconds = 180/);assert.match(text,/timeoutSeconds = \$TimeoutSeconds/);
 for(const token of ['IGAC_FROZEN_OPENVINO_OK:','IGAC_FROZEN_OPENVINO_ASCII_OK:','IGAC_FROZEN_OPENVINO_SOURCE_B64_OK:',
  'openvino=2025.4.1','PYTHONHOME = $null','PYTHONPATH = $null','OPENVINO_LIB_PATHS = $null',
  'PATH = "$SystemDirectory;$WindowsDirectory;$SystemDirectory\\Wbem"','Resolve-Path -LiteralPath $Executable',
  'StartsWith($ExpectedCachePrefix','FromBase64String','throw $PrimaryFailure'])assert.ok(text.includes(token),token);
});

test('embedded Windows probe keeps stage and overall remainders plus truthful cleanup receipts',()=>{
 const text=read('desktop/tests/embedded-browser.integration.cjs');
 assert.match(text,/overallMs:45\*60\*1000/);
 assert.match(text,/Math\.min\(status\.stageLimitMs-status\.stageElapsedMs,status\.overallLimitMs-status\.totalElapsedMs\)/);
 assert.match(text,/timeoutMs:remainingMs/);assert.match(text,/details\.ownedProbeCleanup=cleanup/);
 assert.match(text,/probe\.cleanupConfirmed===true/);assert.doesNotMatch(text,/spawn\('taskkill\.exe'/);
});

test('PS receives an already range-checked signed exit and retains matching raw DWORD in receipt',()=>{
 const ps=read('scripts/owned_process.ps1'),python=read('scripts/owned_process.py');
 assert.match(ps,/targetExitCodeUnsigned/);assert.match(ps,/ExpectedUnsigned \+= 4294967296/);
 assert.match(ps,/targetExitCode -lt -2147483648/);assert.match(ps,/targetExitCode -gt 2147483647/);
 assert.match(python,/ctypes\.c_int32\(receipt\['targetExitCodeUnsigned'\]\)\.value/);
});
