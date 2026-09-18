@echo off
rem ============================================================================
rem  目录式打包（遗留方式，保留是为了兼容既有流程）
rem
rem  ⚠️ 现在推荐用 build-onefile.bat —— 产出**单个 exe**（自带 Python 运行时，
rem     拷一个文件就能跑）。本脚本产出的是**目录**（dist\BambuMonitor\ 整个文件夹
rem     约 240 MB，需要连同 _internal 目录一起拷贝）。
rem
rem  两者内容相同，只是分发形态不同：
rem    build-onefile.bat  → dist-onefile\BambuMonitor.exe     （单文件，推荐）
rem    build_exe.bat      → dist\BambuMonitor\BambuMonitor.exe（目录，遗留）
rem
rem  说明见 docs\PACKAGING.md
rem ============================================================================
chcp 65001 >nul
rem pip 读 requirements 时用本地编码：中文 Windows（cp936）会因此报
rem UnicodeDecodeError，统一开 UTF-8 模式（见 run.bat 里的同类注释）
set "PYTHONUTF8=1"
setlocal
cd /d "%~dp0"
echo [注意] 这是遗留的目录式打包；单文件请用 build-onefile.bat
echo.
echo 正在安装 PyInstaller 并打包为 exe（输出到 dist\BambuMonitor）...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check pyinstaller
if errorlevel 1 goto :failed

rem ⚠️ 打包参数统一写在 BambuMonitor.spec 里，这里只调用它。
rem 以前这些参数是**写在本文件里**的（--hidden-import ...），于是同一件事有两处定义：
rem spec 里列了 app.core/app.adapters，bat 里没有（或反过来），
rem 结果就是"目录版接第三方设备族会 ImportError、单文件版正常"这类只在特定场景炸的问题。
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean "%~dp0BambuMonitor.spec"
if errorlevel 1 goto :failed

echo.
echo 打包完成：dist\BambuMonitor\BambuMonitor.exe
echo （整个 dist\BambuMonitor 文件夹都要拷走才能运行）
pause
exit /b 0

:failed
echo 打包失败，请检查上方错误信息。
pause
exit /b 1
