@echo off
set "PROJECT_DIR=%~dp0"
set "PYTHON_EXE="

if exist "%PROJECT_DIR%.venv\Scripts\python.exe" set "PYTHON_EXE=%PROJECT_DIR%.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%PROJECT_DIR%..\autodl_gpu_watcher_v0.4.7\.venv\Scripts\python.exe" set "PYTHON_EXE=%PROJECT_DIR%..\autodl_gpu_watcher_v0.4.7\.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "D:\Dev\Anaconda\envs\autodl-watcher\python.exe" set "PYTHON_EXE=D:\Dev\Anaconda\envs\autodl-watcher\python.exe"
if not defined PYTHON_EXE if exist "%USERPROFILE%\anaconda3\envs\autodl-watcher\python.exe" set "PYTHON_EXE=%USERPROFILE%\anaconda3\envs\autodl-watcher\python.exe"
if not defined PYTHON_EXE if exist "%USERPROFILE%\miniconda3\envs\autodl-watcher\python.exe" set "PYTHON_EXE=%USERPROFILE%\miniconda3\envs\autodl-watcher\python.exe"

if not defined PYTHON_EXE (
  echo ERROR: autodl-watcher Python environment was not found.
  echo Expected one of:
  echo   1. %PROJECT_DIR%.venv\Scripts\python.exe
  echo   2. sibling v0.4.7 .venv
  echo   3. D:\Dev\Anaconda\envs\autodl-watcher\python.exe
  echo.
  echo You can create .venv with: py -3.13 -m venv .venv
  exit /b 1
)

set "HTTP_PROXY=http://127.0.0.1:7897"
set "HTTPS_PROXY=http://127.0.0.1:7897"
set "NO_PROXY=localhost,127.0.0.1"
cd /d "%PROJECT_DIR%"
exit /b 0
