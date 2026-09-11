#!/usr/bin/env bash
# 拓竹打印机监控台 · Linux 安装脚本（默认只装服务端依赖，不含 Qt）
#
#   ./linux/install.sh              # 服务端模式（推荐：服务器/NAS/树莓派）
#   ./linux/install.sh --with-gui   # 额外安装 PySide6，可在 Linux 桌面上跑图形界面
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
WITH_GUI=0
for arg in "$@"; do
    case "$arg" in
        --with-gui) WITH_GUI=1 ;;
        *) echo "未知参数：$arg"; exit 2 ;;
    esac
done

PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || { echo "未找到 python3，请先安装：sudo apt install python3 python3-venv"; exit 1; }

echo "[1/3] 创建虚拟环境 .venv ..."
"$PYTHON" -m venv .venv

echo "[2/3] 安装服务端依赖（paho-mqtt / opencv-headless）..."
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements-server.txt

if [ "$WITH_GUI" = "1" ]; then
    echo "      额外安装图形界面依赖 PySide6 ..."
    ./.venv/bin/python -m pip install "PySide6-Essentials>=6.5"
fi

echo "[3/3] 完成。"
echo
echo "启动无界面服务："
echo "    ./.venv/bin/python -m app.headless --port 8080"
echo
echo "首次使用建议先搜索打印机并填入访问代码："
echo "    ./.venv/bin/python -m app.headless --discover"
echo "    ./.venv/bin/python -m app.headless --add-printer \"X2D 192.168.31.110 12345678\""
if [ "$WITH_GUI" = "1" ]; then
    echo
    echo "图形界面（需要桌面环境）："
    echo "    ./.venv/bin/python -m app"
fi
echo
echo "配置文件目录：\${BAMBU_MONITOR_CONFIG_DIR:-~/.config 之外的默认位置}"
echo "如需指定：export BAMBU_MONITOR_CONFIG_DIR=/opt/bambu-monitor/data"
