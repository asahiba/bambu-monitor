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
setlocal
cd /d "%~dp0"
echo [注意] 这是遗留的目录式打包；单文件请用 build-onefile.bat
echo.
echo 正在安装 PyInstaller 并打包为 exe（输出到 dist\BambuMonitor）...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check pyinstaller
if errorlevel 1 goto :failed

".venv\Scripts\python.exe" -m PyInstaller ^
  --noconfirm --clean --windowed ^
  --name BambuMonitor ^
  --add-data "app\bambu\certs;app\bambu\certs" ^
  --add-data "app\bambu\data;app\bambu\data" ^
  --hidden-import paho.mqtt.client ^
  --hidden-import cv2 ^
  --hidden-import app.core.registry ^
  --hidden-import app.adapters.moonraker.adapter ^
  run_app.py
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
