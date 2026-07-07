@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   打包脚本
echo ============================================================

REM 检查 venv
if not exist ".venv\Scripts\python.exe" (
    echo [!] 未找到 .venv，正在创建虚拟环境...
    python -m venv .venv
    echo [!] 正在安装依赖...
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install pywebview pystray pillow requests pycryptodome pyinstaller
)

REM 清理旧产物
echo [*] 清理旧构建产物...
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
del *.spec 2>nul

REM 生成图标
if not exist "app.ico" (
    echo [*] 生成应用图标...
    .venv\Scripts\python.exe make_icon.py
)

REM 打包
echo [*] 开始打包...
.venv\Scripts\python.exe -m PyInstaller ^
    --noconfirm ^
    --onefile ^
    --windowed ^
    --name "HUST校园网助手" ^
    --icon "app.ico" ^
    --add-data "webview_index.html;." ^
    --add-data "config.example.ini;." ^
    --hidden-import "clr_loader" ^
    --hidden-import "pythonnet" ^
    --hidden-import "webview.platforms.edgechromium" ^
    --hidden-import "webview.platforms.winforms" ^
    --collect-all "webview" ^
    webview_app.py

if exist "dist\HUST校园网助手.exe" (
    echo.
    echo ============================================================
    echo   打包成功！
    echo   产物：dist\HUST校园网助手.exe
    echo ============================================================
    echo   使用方法：
    echo     1. 把 HUST校园网助手.exe 和 config.ini 放在同一目录
    echo     2. 编辑 config.ini 填入账号密码
    echo     3. 双击 HUST校园网助手.exe
    echo ============================================================
) else (
    echo.
    echo [X] 打包失败，请检查上方错误信息。
)

pause
