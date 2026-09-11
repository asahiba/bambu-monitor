@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] 首次运行：正在创建虚拟环境 .venv ...
    python -m venv .venv
    if errorlevel 1 goto :failed
    echo [2/2] 正在安装依赖，请稍候（约 2-5 分钟）...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 goto :failed
)

".venv\Scripts\python.exe" -m app %*
exit /b %errorlevel%

:failed
echo.
echo 依赖安装失败。请检查网络后手动执行：
echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
pause
exit /b 1
