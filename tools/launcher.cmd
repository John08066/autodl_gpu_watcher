@echo off
for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI\"
set "PYTHON_EXE="

if exist "%PROJECT_DIR%.venv\Scripts\python.exe" set "PYTHON_EXE=%PROJECT_DIR%.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "D:\Dev\Anaconda\envs\autodl-watcher\python.exe" set "PYTHON_EXE=D:\Dev\Anaconda\envs\autodl-watcher\python.exe"
if not defined PYTHON_EXE if exist "%USERPROFILE%\anaconda3\envs\autodl-watcher\python.exe" set "PYTHON_EXE=%USERPROFILE%\anaconda3\envs\autodl-watcher\python.exe"
if not defined PYTHON_EXE if exist "%USERPROFILE%\miniconda3\envs\autodl-watcher\python.exe" set "PYTHON_EXE=%USERPROFILE%\miniconda3\envs\autodl-watcher\python.exe"
if not defined PYTHON_EXE if exist "%PROJECT_DIR%..\autodl_gpu_watcher_v0.4.7\.venv\Scripts\python.exe" set "PYTHON_EXE=%PROJECT_DIR%..\autodl_gpu_watcher_v0.4.7\.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%PROJECT_DIR%..\autodl_gpu_watcher_v0.4.9\.venv\Scripts\python.exe" set "PYTHON_EXE=%PROJECT_DIR%..\autodl_gpu_watcher_v0.4.9\.venv\Scripts\python.exe"

if not defined PYTHON_EXE (
  echo ERROR: autodl-watcher Python environment was not found.
  echo.
  echo Checked project .venv, the workstation Conda environment,
  echo common Anaconda/Miniconda locations, and the old laptop .venv.
  echo.
  echo Create a local environment with:
  echo   py -3.13 -m venv .venv
  exit /b 1
)

set "HTTP_PROXY=http://127.0.0.1:7897"
set "HTTPS_PROXY=http://127.0.0.1:7897"
set "NO_PROXY=localhost,127.0.0.1"
cd /d "%PROJECT_DIR%"
exit /b 0
