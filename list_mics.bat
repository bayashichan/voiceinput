@echo off
cd /d "%~dp0"

set PYTHON=
python --version > nul 2>&1
if not errorlevel 1 set PYTHON=python
if "%PYTHON%"=="" (
    py --version > nul 2>&1
    if not errorlevel 1 set PYTHON=py
)
if "%PYTHON%"=="" (
    echo Python not found.
    pause
    exit /b 1
)

%PYTHON% list_mics.py
