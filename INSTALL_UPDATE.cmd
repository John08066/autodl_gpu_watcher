@echo off
call "%~dp0_launcher.cmd"
if errorlevel 1 pause & exit /b 1
echo Installing current source into:
echo %PYTHON_EXE%
"%PYTHON_EXE%" -m pip install -e . --no-build-isolation --no-deps
if errorlevel 1 (
  echo.
  echo Install failed. Retry with network-enabled dependency install if needed:
  echo "%PYTHON_EXE%" -m pip install -e .
  pause
  exit /b 1
)
"%PYTHON_EXE%" -c "import autodl_watcher; print('Installed version:', autodl_watcher.__version__); print(autodl_watcher.__file__)"
pause
