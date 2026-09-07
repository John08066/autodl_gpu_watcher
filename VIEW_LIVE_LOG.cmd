@echo off
call "%~dp0_launcher.cmd"
if errorlevel 1 pause & exit /b 1
set "LOG_FILE=%PROJECT_DIR%..\autodl_watcher_runtime\logs\watcher.log"
if not exist "%LOG_FILE%" (
  echo ERROR: log file not found:
  echo %LOG_FILE%
  pause
  exit /b 1
)
echo Press Ctrl+C to stop viewing.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -LiteralPath '%LOG_FILE%' -Tail 80 -Wait"
