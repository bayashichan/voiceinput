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

echo.
echo Available microphones:
echo.
%PYTHON% -c "import sounddevice as sd; devs=sd.query_devices(); default=sd.query_devices(kind='input')['name']; [print('  [%d] %s%s' % (i, d['name'], ' <-- default' if d['name']==default else '')) for i,d in enumerate(devs) if d['max_input_channels']>0]; print()"
echo.
echo To use a specific mic: open .env and uncomment/set MICROPHONE_INDEX=^<number^>
echo Example: MICROPHONE_INDEX=1
echo.
pause
