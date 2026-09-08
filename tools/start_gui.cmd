@echo off
call "%~dp0launcher.cmd"
if errorlevel 1 exit /b 1
set "PYTHONW_EXE=%PYTHON_EXE:python.exe=pythonw.exe%"
if not exist "%PYTHONW_EXE%" exit /b 1
"%PYTHONW_EXE%" "%PROJECT_DIR%tools\gui_entry.py"
exit /b %errorlevel%
