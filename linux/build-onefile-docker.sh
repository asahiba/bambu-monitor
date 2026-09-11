#!/usr/bin/env bash
# 用 Docker 容器构建 Linux 单文件产物（宿主机不需要是 Linux）。
#
# 用法（在 Windows/macOS/Linux 任一有 Docker 的机器上）：
#     bash linux/build-onefile-docker.sh
#
# 为什么可行：PyInstaller **不能跨平台编译**，但容器本身就是 Linux，
# 于是"在容器里构建、把产物拷出来"就绕过了这个限制，
# 而且构建环境与 Docker 部署环境完全一致。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

IMAGE="${BAMBU_BUILD_IMAGE:-bambu-monitor-linux-build}"
CONTAINER="bambu-linux-build-$$"

echo "=== 1/3 构建构建镜像（含 PySide6 + PyInstaller）==="
docker build -f linux/Dockerfile.build -t "$IMAGE" .

echo "=== 2/3 在容器内执行 PyInstaller ==="
# ⚠️ 在 Git Bash / MSYS 下必须关掉路径转换，否则 `-w /src` 会被改写成
# `C:/Program Files/Git/src` 导致 docker 报 "working directory is invalid"
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL="*"
docker run --rm --name "$CONTAINER" \
  -v "$ROOT:/src" \
  -w /src \
  "$IMAGE" \
  bash -lc "pip install --quiet pyinstaller && \
            rm -rf build-onefile-linux dist-onefile-linux && \
            python -m PyInstaller --noconfirm --clean \
              --distpath /src/dist-onefile-linux \
              --workpath /src/build-onefile-linux \
              /src/BambuMonitor-linux.spec && \
            chmod +x /src/dist-onefile-linux/BambuMonitor-linux && \
            /src/dist-onefile-linux/BambuMonitor-linux --version"

echo "=== 3/3 产物 ==="
ls -lh "$ROOT/dist-onefile-linux/BambuMonitor-linux"
echo
echo "构建完成：$ROOT/dist-onefile-linux/BambuMonitor-linux"
echo "这是一个**自带 Python 运行时**的单个可执行文件，目标机无需安装任何东西。"
