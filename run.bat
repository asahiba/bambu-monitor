@echo off
rem 启动监控台（GUI）。用法：run.bat [传给 app 的参数]
rem   首次运行会自动创建 .venv 并安装依赖。
chcp 65001 >nul
rem ⚠️ 必须让 Python 走 UTF-8 模式：requirements.txt 里有中文注释，
rem 而中文 Windows（cp936）下 **pip 会用本地编码去读它**，直接报
rem     UnicodeDecodeError: 'gbk' codec can't decode byte ... in position ...
rem 装依赖的第一步就挂（已实测复现）。Linux 与 CI 本来就是 UTF-8，不受影响。
set "PYTHONUTF8=1"
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"

rem ── 环境自愈 ────────────────────────────────────────────────────────────
rem 注意：**不能**只判断 python.exe 文件是否存在。本项目历史上搬过目录
rem （D:\DSH\... -> L:\DSH\...）而基础 Python 又被删掉，此时
rem .venv\Scripts\python.exe 仍在、却一启动就退出（退出码 103，
rem 报 "did not find executable at ..."）。所以这里真的启动一次解释器，
rem 启动不了就重建虚拟环境 —— 与 test.bat 用的是同一条判据。
if not exist "%PY%" goto :setup

"%PY%" -c "import sys" >nul 2>&1
if not errorlevel 1 goto :run

echo [!] .venv 存在但无法启动（基础 Python 可能已被移动或删除）。
echo     正在重建虚拟环境...
rmdir /s /q ".venv"
if exist ".venv" (
    echo [!] 无法删除 .venv，请手动删除后重新运行本脚本。
    pause
    exit /b 1
)

:setup
echo [1/2] 首次运行：正在创建虚拟环境 .venv ...
python -m venv .venv
if errorlevel 1 goto :failed
echo [2/2] 正在安装依赖，请稍候（约 2-5 分钟）...
"%PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :failed

:run
"%PY%" -m app %*
exit /b %errorlevel%

:failed
echo.
echo 依赖安装失败。请检查网络后手动执行：
echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
pause
exit /b 1
