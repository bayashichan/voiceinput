@echo off
chcp 65001 > nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ==========================================
echo   Voice Input - セットアップ
echo ==========================================
echo.

:: ---- Python コマンドを自動検出 ----
:: python → py の順に試す（PATH未設定でも py ランチャーが使えることが多い）
set PYTHON=
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

:: どちらも見つからない場合
echo [エラー] Python が見つかりませんでした。
echo.
echo 【対処法】
echo   1. https://www.python.org/downloads/ を開く
echo   2. 「Download Python 3.x.x」をクリック
echo   3. インストーラーを起動する
echo   4. 最初の画面の「Add python.exe to PATH」に必ずチェックを入れる  ★重要
echo   5. 「Install Now」をクリック
echo   6. 完了後、このファイル（setup.bat）を再度ダブルクリックする
echo.
pause
exit /b 1

:check_version
%PYTHON% -c "import sys;exit(0 if sys.version_info>=(3,10) else 1)" > nul 2>&1
if errorlevel 1 (
    echo [エラー] Python 3.10 以上が必要です。
    %PYTHON% --version
    echo   上記のバージョンでは動作しません。
    echo   https://www.python.org/downloads/ から最新版をインストールしてください。
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('%PYTHON% --version') do echo [OK] %%v を確認しました
echo.

:: ---- ライブラリ インストール ----
echo [1/3] 必要なライブラリをインストールしています...
echo       初回は数分かかる場合があります。しばらくお待ちください。
echo.
%PYTHON% -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [エラー] インストールに失敗しました。
    echo 上のエラーメッセージを確認してください。
    pause
    exit /b 1
)
echo.
echo [OK] ライブラリのインストール完了
echo.

:: ---- .env ファイル作成 ----
echo [2/3] 設定ファイルを確認中...
if not exist ".env" (
    copy ".env.example" ".env" > nul
    echo [OK] .env ファイルを作成しました
) else (
    echo [OK] .env ファイルが存在します
)
echo.

:: ---- API キー確認・設定 ----
echo [3/3] Groq API キーを確認中...
findstr /C:"GROQ_API_KEY=gsk_" ".env" > nul 2>&1
if errorlevel 1 (
    echo API キーが未設定です。
    echo.
    echo Groq コンソール（console.groq.com）で取得した
    echo API キー（gsk_ で始まる文字列）を貼り付けて Enter を押してください:
    echo.
    %PYTHON% -c "import re;from pathlib import Path;key=input('  APIキー > ').strip();e=Path('.env').read_text('utf-8');e=re.sub('GROQ_API_KEY=.*','GROQ_API_KEY='+key,e);Path('.env').write_text(e,'utf-8');print();print('[OK] API キーを保存しました')"
    if errorlevel 1 (
        echo [エラー] API キーの保存に失敗しました。
        pause
        exit /b 1
    )
) else (
    echo [OK] API キーは設定済みです
)
echo.

:: ---- 起動コマンドを決定（pythonw = コンソールなし起動）----
set PYTHONW=pythonw
pythonw --version > nul 2>&1
if errorlevel 1 (
    set PYTHONW=%PYTHON%w
    %PYTHON%w --version > nul 2>&1
    if errorlevel 1 set PYTHONW=%PYTHON%
)

:: ---- 完了 ----
echo ==========================================
echo   セットアップ完了！
echo ==========================================
echo.
echo ショートカットキー : Ctrl+Space で録音開始 / 停止
echo 終了方法           : タスクトレイのアイコンを右クリック → 終了
echo.
choice /C YN /M "今すぐ起動しますか？"
if errorlevel 2 goto :done
echo.
echo 起動しています... タスクトレイ（画面右下）を確認してください。
start %PYTHONW% main.py
echo.
:done
echo 次回からは start.bat をダブルクリックで起動できます。
echo.
pause
