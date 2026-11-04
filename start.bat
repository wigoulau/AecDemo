@echo off
rem AEC Demo launcher (Windows)
setlocal
rem %~dp0 already ends with a backslash; append "." so the closing quote is not escaped
cd /d "%~dp0."

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

"%PY%" -c "import fastapi, uvicorn, numpy" >nul 2>&1
if errorlevel 1 (
  echo [AEC] Missing dependencies, installing requirements.txt ...
  "%PY%" -m pip install -r requirements.txt
  if errorlevel 1 goto fail
)

"%PY%" run.py %*
exit /b %errorlevel%

:fail
echo [AEC] Install failed. Run manually: pip install -r requirements.txt
pause
exit /b 1
