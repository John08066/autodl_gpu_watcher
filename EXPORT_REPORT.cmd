@echo off
call "%~dp0_launcher.cmd"
if errorlevel 1 pause & exit /b 1
"%PYTHON_EXE%" -m autodl_watcher.usage_report
set "LATEST_FILE=%PROJECT_DIR%..\autodl_watcher_runtime\usage\exports\LATEST_EXPORT.txt"
if not exist "%LATEST_FILE%" (
  echo ERROR: report was not generated.
  pause
  exit /b 1
)
set /p LATEST_DIR=<"%LATEST_FILE%"
explorer "%LATEST_DIR%"
pause
