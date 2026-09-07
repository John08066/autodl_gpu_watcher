@echo off
call "%~dp0_launcher.cmd"
if errorlevel 1 pause & exit /b 1
echo Closing Edge processes used by watcher login/browser profiles...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$root=(Resolve-Path '%PROJECT_DIR%..\autodl_watcher_runtime').Path; Get-CimInstance Win32_Process -ErrorAction SilentlyContinue ^| Where-Object { $PSItem.Name -eq 'msedge.exe' -and $PSItem.CommandLine -and ($PSItem.CommandLine.Contains($root + '\browser_profile') -or $PSItem.CommandLine.Contains($root + '\login_profile')) } ^| ForEach-Object { Stop-Process -Id $PSItem.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 2 /nobreak >nul
echo Cleanup finished.
pause
