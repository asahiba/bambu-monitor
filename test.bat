@echo off
rem 跑自动化测试（pytest）。用法：test.bat [pytest 参数]
rem   例：test.bat -k web        只跑名字含 web 的用例
rem       test.bat -m slow       只跑端到端慢测试（需先 set BAMBU_RUN_SLOW=1）
setlocal
cd /d "%~dp0"

rem pip 读 requirements 时用本地编码：中文 Windows（cp936）会因此报
rem UnicodeDecodeError（本项目 requirements 里有中文注释），统一开 UTF-8 模式
set "PYTHONUTF8=1"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [!] 没找到 %PY%，请先运行 run.bat 或手动创建虚拟环境。
    pause
    exit /b 1
)

rem 确认解释器真的可用：虚拟环境失效时 python.exe 仍然存在但无法启动
"%PY%" -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo [!] 虚拟环境不可用（基础 Python 解释器可能已被移动或删除）。
    echo     请删除 .venv 目录后重新运行 run.bat。
    pause
    exit /b 1
)

"%PY%" -c "import pytest" >nul 2>&1
if errorlevel 1 (
    echo [*] 正在安装测试依赖...
    "%PY%" -m pip install -r requirements-dev.txt
    if errorlevel 1 (
        echo [!] 依赖安装失败，请检查网络后重试。
        pause
        exit /b 1
    )
)

"%PY%" -m pytest %*
set "CODE=%ERRORLEVEL%"
echo.
if "%CODE%"=="0" (echo 测试全部通过 ✓) else (echo 存在失败用例 ✗ ^(退出码 %CODE%^))
pause
exit /b %CODE%
