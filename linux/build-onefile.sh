#!/usr/bin/env bash
# 在 Linux 上构建单文件产物（带界面版，含 Qt）。
#
# 用法（在 Linux 机器 / WSL 里执行）：
#     bash linux/build-onefile.sh
#
# 宿主机不是 Linux 时，请改用 linux/build-onefile-docker.sh（用容器构建）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV="${BAMBU_BUILD_VENV:-$ROOT/.venv-linux}"
PY="$VENV/bin/python"

echo "=== 1/4 准备虚拟环境 ==="
if [ ! -x "$PY" ]; then
  if ! python3 -m venv "$VENV" 2>/dev/null; then
    echo "[!] 创建虚拟环境失败。Debian/Ubuntu 需要先装：" >&2
    echo "      sudo apt-get install -y python3-venv python3-pip" >&2
    exit 1
  fi
fi
"$PY" -m pip install --quiet --upgrade pip

echo "=== 2/4 安装依赖（含 PySide6，用于图形界面）==="
"$PY" -m pip install --quiet -r requirements.txt pyinstaller

echo "=== 3/4 依赖自检 ==="
"$PY" - <<'PYCODE'
import importlib
for name in ("PyInstaller", "PySide6.QtWidgets", "paho.mqtt.client", "cv2", "cryptography", "app.main"):
    importlib.import_module(name)
    print(f"  {name}: ok")
PYCODE

echo "=== 4/4 打包 ==="
rm -rf build-onefile-linux dist-onefile-linux
"$PY" -m PyInstaller --noconfirm --clean \
  --distpath "$ROOT/dist-onefile-linux" \
  --workpath "$ROOT/build-onefile-linux" \
  "$ROOT/BambuMonitor-linux.spec"

ARTIFACT="$ROOT/dist-onefile-linux/BambuMonitor-linux"
chmod +x "$ARTIFACT"
ls -lh "$ARTIFACT"
echo
echo "冒烟验证："
"$ARTIFACT" --version
BAMBU_MONITOR_CONFIG_DIR="$(mktemp -d)" "$ARTIFACT" --list || true
echo
echo "构建完成：$ARTIFACT"
echo "提示：无显示器的服务器上请加 --headless 运行服务模式。"
