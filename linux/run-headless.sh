#!/usr/bin/env bash
# 启动无界面服务（Linux / NAS / 树莓派）
#
#   ./linux/run-headless.sh                # 用配置文件里的打印机，监听 8080
#   ./linux/run-headless.sh --sim 4        # 演示模式：4 台虚拟打印机
#   BAMBU_WEB_PORT=9000 ./linux/run-headless.sh
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -x ".venv/bin/python" ]; then
    echo "尚未安装依赖，正在执行 linux/install.sh ..."
    ./linux/install.sh
fi

export BAMBU_MONITOR_CONFIG_DIR="${BAMBU_MONITOR_CONFIG_DIR:-$(pwd)/data}"
mkdir -p "$BAMBU_MONITOR_CONFIG_DIR"

exec ./.venv/bin/python -m app.headless "$@"
