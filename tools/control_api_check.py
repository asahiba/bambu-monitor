"""开发用：测试**控制接口** /api/command。

⚠️ 会真的下发控制指令（暂停/继续/停止/开关灯）：
   只能对「内置模拟器」或「你自己明确愿意被控制的打印机」使用。
   真实打印机正在打印时请勿运行。

用法：``python tools/control_api_check.py <端口> [访问令牌] [画面序号]``
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from app.config import AppConfig  # noqa: E402
from tools._common import enable_utf8  # noqa: E402

enable_utf8()

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
token = sys.argv[2] if len(sys.argv) > 2 else AppConfig.load().web_token
index = int(sys.argv[3]) if len(sys.argv) > 3 else 0
base = f"http://127.0.0.1:{port}"

print(f"目标 {base} 画面 #{index}（令牌 {len(token)} 位）")
print("⚠️ 仅对模拟器使用；真实打印机请勿运行本脚本。\n")

ok = True


def status_of(index: int) -> dict:
    with urllib.request.urlopen(f"{base}/api/printers?token={token}", timeout=8) as response:
        data = json.loads(response.read().decode("utf-8"))
    printers = data.get("printers", [])
    if not 0 <= index < len(printers):
        raise SystemExit(f"画面 #{index} 不存在（共 {len(printers)} 台）")
    return printers[index]


def post(action: str, value: str = "") -> tuple[int, dict]:
    payload = json.dumps({"index": index, "action": action, "value": value}).encode()
    request = urllib.request.Request(
        f"{base}/api/command?token={token}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


before = status_of(index)
print(f"① 当前状态：{before['name']} {before['state_text']} {before['progress']}% "
      f"舱灯={before['light']} 可控={before['can_control']}")
print(f"   耗材：AMS {len(before['ams'])} 槽，外挂={before['external']}")
print(f"   环境：仓温={before['chamber']}℃ WiFi={before['wifi']}({before['wifi_level']}格) "
      f"预计完成={before['finish_time']} HMS={len(before['hms'])} 条")
if not before["can_control"]:
    print("✗ 遥测未连接，无法测试控制接口")
    raise SystemExit(1)

print("\n② 顺序下发指令并回读状态")
sequence = [
    ("light_off", "", "舱灯=off"),
    ("light_on", "", "舱灯=on"),
    ("pause", "", "已暂停"),
    ("resume", "", "打印中"),
]
for action, value, expect in sequence:
    code, result = post(action, value)
    time.sleep(1.5)
    after = status_of(index)
    print(f"   {action:11} -> {code} {result.get('detail')}  回读：状态={after['state_text']} "
          f"舱灯={after['light']}")
    if not result.get("ok"):
        ok = False

print("\n③ 停止指令（模拟器上验证；真机请勿执行）")
code, result = post("stop")
time.sleep(2)
after = status_of(index)
print(f"   stop -> {code} {result.get('detail')}  回读：状态={after['state_text']} {after['progress']}%")
if not result.get("ok") or after["progress"] != 0:
    ok = False

print("\n④ 无令牌应被拒绝")
try:
    urllib.request.urlopen(
        urllib.request.Request(
            f"{base}/api/command",
            data=b'{"index":0,"action":"pause"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        ),
        timeout=5,
    )
    print("   ✗ 未被拒绝")
    ok = False
except urllib.error.HTTPError as exc:
    print(f"   -> {exc.code} ✓")

print("\n" + "=" * 58)
print("控制接口检查：" + ("通过 ✓" if ok else "存在失败项 ✗"))
print("=" * 58)
sys.exit(0 if ok else 1)
