@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ==========================================
echo   Voice Input - Setup
echo ==========================================
echo.

:: ---- Detect Python ----
set PYTHON=
set PYTHONW=
python --version > nul 2>&1
if not errorlevel 1 (
    set PYTHON=python
    goto :check_version
)
py --version > nul 2>&1
if not errorlevel 1 (
    set PYTHON=py
    goto :check_version
)
echo [ERROR] Python not found.
echo.
echo Install from: https://www.python.org/downloads/
echo IMPORTANT: Check "Add python.exe to PATH" during install.
echo.
pause
exit /b 1

:check_version
%PYTHON% -c "import sys;exit(0 if sys.version_info>=(3,10) else 1)" > nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.10+ required.
    %PYTHON% --version
    pause
    exit /b 1
)
for /f "delims=" %%v in ('%PYTHON% --version') do echo [OK] %%v found

:: Locate pythonw (console-less launcher)
for /f "delims=" %%p in ('where %PYTHON%w 2^>nul') do set PYTHONW=%%p
if "!PYTHONW!"=="" for /f "delims=" %%p in ('where %PYTHON% 2^>nul') do set PYTHONW=%%p
echo.

:: ---- Install libraries ----
echo [1/4] Installing libraries (first run may take a few minutes)...
echo.
%PYTHON% -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Installation failed. See message above.
    pause
    exit /b 1
)
echo.
echo [OK] Libraries installed
echo.

:: ---- Create .env ----
echo [2/4] Checking config file...
if not exist ".env" (
    copy ".env.example" ".env" > nul
    echo [OK] Created .env
) else (
    echo [OK] .env already exists
)
echo.

:: ---- API key ----
echo [3/4] Checking Groq API key...
findstr /C:"GROQ_API_KEY=gsk_" ".env" > nul 2>&1
if errorlevel 1 (
    echo API key not set.
    echo.
    echo Paste your Groq API key (starts with gsk_) then press Enter:
    echo.
    %PYTHON% -c "import re;from pathlib import Path;key=input('  API key > ').strip();e=Path('.env').read_text('utf-8');e=re.sub('GROQ_API_KEY=.*','GROQ_API_KEY='+key,e);Path('.env').write_text(e,'utf-8');print('[OK] API key saved')"
    if errorlevel 1 (
        echo [ERROR] Failed to save API key.
        pause
        exit /b 1
    )
) else (
    echo [OK] API key already set
)
echo.

:: ---- Startup registration ----
echo [4/4] Windows startup registration...
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LNK_PATH=!STARTUP_DIR!\VoiceInput.lnk"
set "APP_DIR=%CD%"

if exist "!LNK_PATH!" (
    echo [OK] Already registered for startup
) else (
    choice /C YN /M "Register Voice Input to launch automatically with Windows?"
    if not errorlevel 2 (
        powershell -Command "$wsh=New-Object -ComObject WScript.Shell;$lnk=$wsh.CreateShortcut('!LNK_PATH!');$lnk.TargetPath='!PYTHONW!';$lnk.Arguments='\"!APP_DIR!\main.py\"';$lnk.WorkingDirectory='!APP_DIR!';$lnk.WindowStyle=7;$lnk.Save()" > nul 2>&1
        if exist "!LNK_PATH!" (
            echo [OK] Registered for startup
        ) else (
            echo [WARN] Could not create shortcut. Manually copy start.bat to:
            echo        !STARTUP_DIR!
        )
    ) else (
        echo Skipped.
    )
)
echo.

:: ---- Done ----
echo ==========================================
echo   Setup complete!
echo ==========================================
echo.
echo Hotkey : Ctrl+Space to start/stop recording
echo Quit   : Right-click the tray icon, then Quit
echo Mic    : Run list_mics.bat to see device numbers
echo         Then set MICROPHONE_INDEX=^<number^> in .env
echo.
choice /C YN /M "Launch Voice Input now?"
if errorlevel 2 goto :done
echo.
echo Launching... Check the system tray (bottom-right).
start "VoiceInput" "!PYTHONW!" "!APP_DIR!\main.py"
echo.
:done
echo Use start.bat to launch next time.
echo.
pause
