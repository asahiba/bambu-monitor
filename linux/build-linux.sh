#!/usr/bin/env bash
# 在 Linux 上打包成单文件可执行程序（必须在 Linux 机器上运行；Windows 的 PyInstaller 不能交叉编译）
#
#   ./linux/build-linux.sh
# 产物：dist/BambuMonitor（单文件，~90 MB，含 Python 运行时，不需要目标机装 Python）
set -euo pipefail

cd "$(dirname "$0")/.."

./.venv/bin/python -m pip install --upgrade pyinstaller
./.venv/bin/python -m PyInstaller \
    --noconfirm --clean --onefile \
    --name BambuMonitor \
    --hidden-import paho.mqtt.client \
    --hidden-import cv2 \
    --collect-submodules app \
    run_headless.py

echo
echo "完成：dist/BambuMonitor"
echo "运行：./dist/BambuMonitor --port 8080"
