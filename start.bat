@echo off
cd /d "%~dp0"

where pythonw > nul 2>&1
if not errorlevel 1 (
    start "VoiceInput" pythonw main.py
    exit /b 0
)
where pyw > nul 2>&1
if not errorlevel 1 (
    start "VoiceInput" pyw main.py
    exit /b 0
)
python main.py
