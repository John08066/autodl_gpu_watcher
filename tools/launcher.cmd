@echo off
for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI\"
set "PYTHON_EXE="

if exist "%PROJECT_DIR%.venv\Scripts\python.exe" set "PYTHON_EXE=%PROJECT_DIR%.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "D:\Dev\Anaconda\envs\autodl-watcher\python.exe" set "PYTHON_EXE=D:\Dev\Anaconda\envs\autodl-watcher\python.exe"
if not defined PYTHON_EXE if exist "%USERPROFILE%\anaconda3\envs\autodl-watcher\python.exe" set "PYTHON_EXE=%USERPROFILE%\anaconda3\envs\autodl-watcher\python.exe"
if not defined PYTHON_EXE if exist "%USERPROFILE%\miniconda3\envs\autodl-watcher\python.exe" set "PYTHON_EXE=%USERPROFILE%\miniconda3\envs\autodl-watcher\python.exe"
if not defined PYTHON_EXE (
  echo ERROR: autodl-watcher Python environment was not found.
  echo.
  echo Checked project .venv, the workstation Conda environment,
  echo common Anaconda/Miniconda locations.
  echo.
  echo Create a local environment with:
  echo   py -3.13 -m venv .venv
  exit /b 1
)

set "HTTP_PROXY="
set "HTTPS_PROXY="
set "NO_PROXY=localhost,127.0.0.1,private.autodl.com,.autodl.com"

rem Use Clash only when the local proxy port is actually listening.
"%PYTHON_EXE%" -c "import socket,sys;s=socket.socket();s.settimeout(0.25);r=s.connect_ex(('127.0.0.1',7897));s.close();sys.exit(0 if r==0 else 1)" >nul 2>&1
if not errorlevel 1 (
  set "HTTP_PROXY=http://127.0.0.1:7897"
  set "HTTPS_PROXY=http://127.0.0.1:7897"
)

cd /d "%PROJECT_DIR%"
set "PYTHONPATH=%PROJECT_DIR%src;%PYTHONPATH%"
set "PYTHONUTF8=1"
exit /b 0
