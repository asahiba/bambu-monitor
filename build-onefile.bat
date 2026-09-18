@echo off
rem 构建 Windows 单文件产物（自带 Python 运行时，目标机无需装任何东西）
rem
rem 用法：双击本文件，或在命令行执行 build-onefile.bat
rem 产物：dist-onefile\BambuMonitor.exe
rem
rem 说明：打包成 --windowed 后没有控制台，因此 --help / --core-test 的输出
rem       不会显示在控制台里（程序内部有兜底，不会崩）。要看命令行输出请用源码运行，
rem       或查看 %APPDATA%\BambuMonitor\logs\app.log。详见 docs\PACKAGING.md。
setlocal
cd /d "%~dp0"

rem pip 读 requirements 时用本地编码：中文 Windows（cp936）会因此报
rem UnicodeDecodeError（本项目 requirements 里有中文注释），统一开 UTF-8 模式
set "PYTHONUTF8=1"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [!] 没找到 %PY%，请先运行 run.bat 创建虚拟环境。
    pause
    exit /b 1
)

rem 虚拟环境有可能"文件存在但解释器不可用"（基础 Python 被移动/删除），
rem 所以这里真的启动一次来判断，而不是只看文件是否存在
"%PY%" -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo [!] 虚拟环境不可用（基础 Python 解释器可能已被移动或删除）。
    echo     请删除 .venv 目录后重新运行 run.bat。
    pause
    exit /b 1
)

"%PY%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [*] 正在安装 PyInstaller...
    "%PY%" -m pip install pyinstaller
    if errorlevel 1 (
        echo [!] 安装失败，请检查网络后重试。
        pause
        exit /b 1
    )
)

echo [*] 开始打包（单文件模式，首次约 1-3 分钟）...
"%PY%" -m PyInstaller --noconfirm --clean ^
    --distpath "%~dp0dist-onefile" ^
    --workpath "%~dp0build-onefile" ^
    "%~dp0BambuMonitor-onefile.spec"
if errorlevel 1 (
    echo [!] 打包失败，请检查上面的输出。
    pause
    exit /b 1
)

echo.
echo [*] 验证产物...
"%~dp0dist-onefile\BambuMonitor.exe" --version
if errorlevel 1 (
    echo [!] 产物无法运行。
    pause
    exit /b 1
)

echo.
for %%F in ("%~dp0dist-onefile\BambuMonitor.exe") do echo 构建完成：%%~fF  (%%~zF 字节)
echo 把这个 exe 拷到任何 Windows 电脑即可运行，无需安装 Python。
echo.
pause
