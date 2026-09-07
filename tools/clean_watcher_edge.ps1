$ErrorActionPreference = 'SilentlyContinue'

$projectDir = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path (Split-Path -Parent $projectDir) 'autodl_watcher_runtime'
$profiles = @(
    (Join-Path $runtimeDir 'browser_profile'),
    (Join-Path $runtimeDir 'native_login_profile'),
    (Join-Path $runtimeDir 'login_profile')
)

Write-Host 'Closing Edge processes used by watcher profiles...'

$processes = @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -eq 'msedge.exe' -and $_.CommandLine -and (
        $_.CommandLine.Contains($profiles[0]) -or
        $_.CommandLine.Contains($profiles[1]) -or
        $_.CommandLine.Contains($profiles[2])
    )
})

foreach ($process in $processes) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 2

foreach ($profile in $profiles) {
    if (Test-Path $profile) {
        Get-ChildItem -LiteralPath $profile -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like 'Singleton*' -or $_.Name -eq 'lockfile' -or $_.Name -eq 'DevToolsActivePort' } |
            Remove-Item -Force -Recurse -ErrorAction SilentlyContinue
    }
}

$remaining = @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -eq 'msedge.exe' -and $_.CommandLine -and (
        $_.CommandLine.Contains($profiles[0]) -or
        $_.CommandLine.Contains($profiles[1]) -or
        $_.CommandLine.Contains($profiles[2])
    )
})

if ($remaining.Count -gt 0) {
    Write-Host ('Cleanup incomplete: {0} watcher Edge process(es) still running.' -f $remaining.Count)
    exit 1
}

Write-Host ('Cleanup finished. Matched {0} watcher Edge process(es).' -f $processes.Count)
exit 0
