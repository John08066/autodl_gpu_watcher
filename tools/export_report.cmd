@echo off
call "%~dp0launcher.cmd"
if errorlevel 1 exit /b 1
"%PYTHON_EXE%" -m autodl_watcher.usage_report
if errorlevel 1 exit /b 1
set "LATEST_FILE=%PROJECT_DIR%runtime\usage\exports\LATEST_EXPORT.txt"
if not exist "%LATEST_FILE%" (
  echo ERROR: report was not generated:
  echo %LATEST_FILE%
  exit /b 1
)
set /p LATEST_DIR=<"%LATEST_FILE%"
explorer "%LATEST_DIR%"
exit /b 0
