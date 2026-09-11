#!/usr/bin/env bash
# 跑自动化测试（pytest）。用法：bash linux/run-tests.sh [pytest 参数]
set -euo pipefail

cd "$(dirname "$0")/.."

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "[!] 没找到 $PY，请先运行 bash linux/install.sh" >&2
  exit 1
fi

if ! "$PY" -c "import pytest" >/dev/null 2>&1; then
  echo "[*] 正在安装测试依赖..."
  "$PY" -m pip install -r requirements-dev.txt
fi

exec "$PY" -m pytest "$@"
