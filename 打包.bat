@echo off
cd /d "%~dp0"

echo ============================================================
echo   HUST Net Helper - Build Script
echo ============================================================

REM Always use venv python directly (absolute path), avoid activate.bat
set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [*] Creating venv...
    python -m venv .venv
    echo [*] Installing deps...
    "%PY%" -m pip install --upgrade pip
    "%PY%" -m pip install pyinstaller -r requirements.txt
)

echo [*] Cleaning old build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
del /q *.spec 2>nul

if not exist "app.ico" (
    echo [*] Generating icon...
    "%PY%" make_icon.py
)

echo [*] Building exe...
"%PY%" -m PyInstaller --noconfirm --onefile --windowed --name "HUST_NetHelper" --icon "app.ico" --add-data "webview_index.html;." --add-data "config.example.ini;." --hidden-import "clr_loader" --hidden-import "pythonnet" --hidden-import "webview.platforms.edgechromium" --hidden-import "webview.platforms.winforms" --collect-all "webview" webview_app.py

if exist "dist\HUST_NetHelper.exe" (
    echo.
    echo ============================================================
    echo   SUCCESS: dist\HUST_NetHelper.exe
    echo ============================================================
    echo   Rename it as you like.
) else (
    echo.
    echo   [X] BUILD FAILED
)

pause
