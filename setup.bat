@echo off
chcp 65001 > nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ==========================================
echo   Voice Input - セットアップ
echo ==========================================
echo.

:: ---- Python バージョン確認 ----
python --version > nul 2>&1
if errorlevel 1 (
    echo [エラー] Python が見つかりません。
    echo.
    echo 以下のURLからインストールしてください:
    echo   https://www.python.org/downloads/
    echo.
    echo インストール時に「Add python.exe to PATH」に
    echo チェックを入れることを忘れずに！
    echo.
    pause
    exit /b 1
)
python -c "import sys;exit(0 if sys.version_info>=(3,10) else 1)" > nul 2>&1
if errorlevel 1 (
    echo [エラー] Python 3.10 以上が必要です。
    python --version
    echo   上記バージョンはサポート対象外です。
    echo   https://www.python.org/downloads/ から最新版をインストールしてください。
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python --version') do echo [OK] %%v を確認しました
echo.

:: ---- ライブラリ インストール ----
echo [1/3] 必要なライブラリをインストールしています...
echo       初回は数分かかる場合があります。しばらくお待ちください。
echo.
pip install -r requirements.txt
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
    python -c "import re;from pathlib import Path;key=input('  APIキー > ').strip();e=Path('.env').read_text('utf-8');e=re.sub('GROQ_API_KEY=.*','GROQ_API_KEY='+key,e);Path('.env').write_text(e,'utf-8');print();print('[OK] API キーを保存しました')"
    if errorlevel 1 (
        echo [エラー] API キーの保存に失敗しました。
        pause
        exit /b 1
    )
) else (
    echo [OK] API キーは設定済みです
)
echo.

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
start pythonw main.py
echo.
:done
echo 次回からは start.bat をダブルクリックで起動できます。
echo.
pause
