@echo off
call "%~dp0_launcher.cmd"
if errorlevel 1 pause & exit /b 1
:menu
cls
echo ========================================
echo AutoDL GPU Watcher v0.5.0
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
if errorlevel 9 goto end
if errorlevel 8 call "%~dp0INSTALL_UPDATE.cmd" & goto menu
if errorlevel 7 call "%~dp0CLEAN_WATCHER_EDGE.cmd" & goto menu
if errorlevel 6 call "%~dp0EXPORT_REPORT.cmd" & goto menu
if errorlevel 5 call "%~dp0VIEW_LIVE_LOG.cmd" & goto menu
if errorlevel 4 "%PYTHON_EXE%" -m autodl_watcher.main --dry-run & pause & goto menu
if errorlevel 3 "%PYTHON_EXE%" -m autodl_watcher.main --entry 2 & pause & goto menu
if errorlevel 2 "%PYTHON_EXE%" -m autodl_watcher.main & pause & goto menu
if errorlevel 1 "%PYTHON_EXE%" -m autodl_watcher.login & pause & goto menu
:end
