@echo off
call "%~dp0tools\launcher.cmd"
if errorlevel 1 pause & exit /b 1

:menu
cls
echo ========================================
echo AutoDL GPU Watcher v0.5.1
echo Python: %PYTHON_EXE%
echo ========================================
echo 1. Login / refresh session
echo 2. Start monitor - auto-select entry
echo 3. Start monitor - 203-2 only
echo 4. Dry run - no automatic power-on
echo 5. View live log
echo 6. Export usage report
echo 7. Clean watcher Edge processes
echo 8. Install / update this version
echo 0. Exit
echo ========================================
choice /c 123456780 /n /m "Select: "

if errorlevel 9 goto option0
if errorlevel 8 goto option8
if errorlevel 7 goto option7
if errorlevel 6 goto option6
if errorlevel 5 goto option5
if errorlevel 4 goto option4
if errorlevel 3 goto option3
if errorlevel 2 goto option2
if errorlevel 1 goto option1
goto menu

:option1
"%PYTHON_EXE%" -m autodl_watcher.login
pause
goto menu

:option2
"%PYTHON_EXE%" -m autodl_watcher.main
pause
goto menu

:option3
"%PYTHON_EXE%" -m autodl_watcher.main --entry 2
pause
goto menu

:option4
"%PYTHON_EXE%" -m autodl_watcher.main --dry-run
pause
goto menu

:option5
call "%~dp0tools\view_live_log.cmd"
goto menu

:option6
call "%~dp0tools\export_report.cmd"
pause
goto menu

:option7
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\clean_watcher_edge.ps1"
pause
goto menu

:option8
call "%~dp0tools\install_update.cmd"
pause
goto menu

:option0
exit /b 0
