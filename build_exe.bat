@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
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
  run_app.py
if errorlevel 1 goto :failed

echo.
echo 打包完成：dist\BambuMonitor\BambuMonitor.exe
pause
exit /b 0

:failed
echo 打包失败，请检查上方错误信息。
pause
exit /b 1
