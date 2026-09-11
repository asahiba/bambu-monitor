#!/usr/bin/env bash
# 用 Docker 构建 **无界面版** Linux 单文件产物（不含 Qt）。
#
# 用法：
#     bash linux/build-headless-docker.sh
#
# 与 build-onefile-docker.sh 的区别：那个产出带 Qt 的版本（能开窗口），
# 这个产出不含 Qt 的版本。**服务器 / NAS / 树莓派场景应当用这个**：
#   * 体积更小；
#   * 不依赖图形系统库（缺 libxcb.so.1 的环境下带 Qt 版本会导致
#     模拟器画面生成失败，见 docs/PACKAGING.md）；
#   * 用 --headless 跑网页服务 + PWA，这正是 Linux 部署的既定形态。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

IMAGE="${BAMBU_BUILD_IMAGE:-bambu-monitor-linux-build}"
OUT="$ROOT/dist-onefile-headless"

echo "=== 1/3 准备构建镜像（复用带界面版的构建镜像即可）==="
docker build -f linux/Dockerfile.build -t "$IMAGE" .

echo "=== 2/3 容器内执行 PyInstaller（headless spec，不含 Qt）==="
# ⚠️ Git Bash / MSYS 下必须关掉路径转换，否则 -w /src 会被改写成 Windows 路径
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL="*"
docker run --rm \
  -v "$ROOT:/src" \
  -w /src \
  "$IMAGE" \
  bash -lc "pip install --quiet pyinstaller && \
            rm -rf build-onefile-headless dist-onefile-headless && \
            python -m PyInstaller --noconfirm --clean \
              --distpath /src/dist-onefile-headless \
              --workpath /src/build-onefile-headless \
              /src/BambuMonitor-headless.spec && \
            chmod +x /src/dist-onefile-headless/BambuMonitor-headless"

echo "=== 3/3 冒烟验证：放进干净容器（无 Qt、无任何依赖）==="
docker run --rm -v "$OUT:/app" -e BAMBU_MONITOR_CONFIG_DIR=/tmp/cfg \
  python:3.11-slim /app/BambuMonitor-headless --version

# 端到端验证**真实交付形态**：起服务 → 健康检查 → 退出。
#
# ⚠️ 配置目录用 **Docker 卷**而不是 /tmp：配置要真的能写盘
# （`config.save()` 会建目录并写文件）。指向不存在的路径时
# `AppConfig.load()` 会在 `os.makedirs` 上抛 PermissionError，容器直接就起不来。
echo "--- 端到端 A：空配置也能起服务（Docker 首启场景）---"
docker volume rm -f bambu-cfg-empty >/dev/null 2>&1 || true
docker run --rm -d --name bambu-smoke-empty -p 18080:8080 \
  -v "$OUT:/app:ro" -v bambu-cfg-empty:/data -e BAMBU_MONITOR_CONFIG_DIR=/data \
  python:3.11-slim /app/BambuMonitor-headless >/dev/null
cleanup_empty() {
  docker rm -f bambu-smoke-empty >/dev/null 2>&1 || true
  docker volume rm -f bambu-cfg-empty >/dev/null 2>&1 || true
}

echo "--- 端到端 B：演示模式（模拟打印机 + 网页服务）---"
docker volume rm -f bambu-cfg-sim >/dev/null 2>&1 || true
docker run --rm -d --name bambu-headless-smoke -p 18081:8080 \
  -v "$OUT:/app:ro" -v bambu-cfg-sim:/data -e BAMBU_MONITOR_CONFIG_DIR=/data \
  python:3.11-slim /app/BambuMonitor-headless --headless --sim 2 --status-interval 0 >/dev/null

cleanup() {
  docker rm -f bambu-headless-smoke >/dev/null 2>&1 || true
  docker volume rm -f bambu-cfg-sim >/dev/null 2>&1 || true
  cleanup_empty
}
trap cleanup EXIT

check_health() {
  # $1 = 端口, $2 = 说明
  local port="$1" label="$2"
  for _ in $(seq 1 15); do
    sleep 3
    if curl -fsS "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
      echo "  ${label}: /health ok"
      return 0
    fi
  done
  echo "  ✗ ${label}: /health 未就绪"
  return 1
}

failed=0
check_health 18080 "空配置启动" || failed=1
check_health 18081 "演示模式启动" || failed=1

if [ "$failed" -ne 0 ]; then
  echo "--- 空配置容器日志 ---"; docker logs bambu-smoke-empty 2>&1 | tail -15
  echo "--- 演示容器日志 ---"; docker logs bambu-headless-smoke 2>&1 | tail -15
  exit 1
fi

echo "  演示模式日志尾部："
docker logs bambu-headless-smoke 2>&1 | tail -4

ls -lh "$OUT/BambuMonitor-headless"
echo
echo "构建完成：$OUT/BambuMonitor-headless"
echo "目标机用法：  ./BambuMonitor-headless --headless --port 8080"
