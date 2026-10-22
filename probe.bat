@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Install the project dependencies in .venv first. See README.md.
    pause
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -m tools.desktop_probe %*
if errorlevel 1 pause
