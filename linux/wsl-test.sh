#!/usr/bin/env bash
# 在 WSL / 真实 Linux 上验证无界面服务模式（Linux 版冒烟测试）
#
#   wsl -d Ubuntu -- bash /mnt/d/DSH/bambu-monitor/linux/wsl-test.sh
# 或直接在 Linux 上：./linux/wsl-test.sh
set -uo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${HOME}/bambu-linux-test"
PORT=18077

echo "== 1) 准备环境 =="
echo "源目录: $SRC"
echo "Python: $(python3 --version)"
rm -rf "$WORK"
mkdir -p "$WORK"
cp -r "$SRC/app" "$WORK/"
cp "$SRC/requirements-server.txt" "$WORK/"
cd "$WORK"

python3 -m venv .venv
./.venv/bin/python -m pip install -q --upgrade pip

echo "== 2) 安装服务端依赖（不含 Qt）=="
if ./.venv/bin/python -m pip install -q -r requirements-server.txt; then
    echo "   依赖安装成功"
else
    echo "   ⚠ 依赖安装有失败（可能是该 Python 版本暂无 wheel），继续测试其余部分"
fi

# 确认没有 Qt
if ./.venv/bin/python -c "import PySide6" 2>/dev/null; then
    echo "   ✗ 竟然装上了 PySide6"
else
    echo "   ✓ 环境中没有 PySide6（正是 Linux 无界面模式的前提）"
fi

echo "== 3) 启动无界面服务（4 台模拟打印机）=="
export BAMBU_MONITOR_CONFIG_DIR="$WORK/data"
mkdir -p "$BAMBU_MONITOR_CONFIG_DIR"
./.venv/bin/python -m app.headless --sim 4 --port "$PORT" --sim-code 12345678 --status-interval 0 >"$WORK/server.log" 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

READY=0
for _ in $(seq 1 30); do
    sleep 1
    if ./.venv/bin/python - "$PORT" <<'PY' 2>/dev/null
import sys, urllib.request
port = sys.argv[1]
try:
    sys.exit(0 if urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2).status == 200 else 1)
except Exception:
    sys.exit(1)
PY
    then READY=1; break; fi
done

if [ "$READY" != "1" ]; then
    echo "   ✗ 服务未就绪，日志："
    tail -20 "$WORK/server.log"
    exit 1
fi
echo "   ✓ 服务已就绪：http://127.0.0.1:$PORT"

echo "== 4) 接口与 PWA 资源 =="
./.venv/bin/python - "$PORT" <<'PY'
import json, os, sys, urllib.request
port = sys.argv[1]
base = f"http://127.0.0.1:{port}"
token = ""
cfg = os.path.join(os.environ["BAMBU_MONITOR_CONFIG_DIR"], "config.json")
if os.path.exists(cfg):
    token = json.load(open(cfg, encoding="utf-8")).get("web_token", "")
ok = True
for path, needle in (("/health", b"ok"), ("/", b"wall"), ("/manifest.webmanifest", b"display"),
                     ("/icon-192.png", b"\x89PNG"), ("/sw.js", b"addEventListener")):
    try:
        with urllib.request.urlopen(f"{base}{path}?token={token}", timeout=5) as r:
            body = r.read()
        hit = needle in body
        print(f"   {path:24} {r.status} {len(body):>6} 字节 {'✓' if hit else '✗'}")
        ok = ok and hit
    except Exception as exc:
        print(f"   {path:24} ✗ {exc}")
        ok = False
with urllib.request.urlopen(f"{base}/api/printers?token={token}", timeout=8) as r:
    data = json.loads(r.read())
print(f"   状态接口：共 {data['total']} 台 · 画面在线 {data['camera_online']} · 遥测在线 {data['mqtt_online']}")
if data["total"] != 4:
    ok = False
sys.exit(0 if ok else 1)
PY
RESULT=$?

echo "== 5) 服务日志片段 =="
grep -E "网页监控|打印机：|自检|Error|Traceback" "$WORK/server.log" | head -8 || true

kill $SERVER_PID 2>/dev/null || true
trap - EXIT

if [ "$RESULT" = "0" ]; then
    echo "== Linux 冒烟测试：全部通过 ✓ =="
else
    echo "== Linux 冒烟测试：存在失败项 ✗ =="
fi
exit "$RESULT"
