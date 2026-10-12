@echo off
title TibiaAuto12
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" start.py
) else (
    python start.py
)
if errorlevel 1 pause
