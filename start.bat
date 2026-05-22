@echo off
cd /d "%~dp0"

:: pythonw → pyw の順に試す
pythonw --version > nul 2>&1
if not errorlevel 1 (
    start pythonw main.py
    exit /b 0
)
pyw --version > nul 2>&1
if not errorlevel 1 (
    start pyw main.py
    exit /b 0
)

:: どちらもない場合はコンソールありで起動
python main.py
