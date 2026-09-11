"""开发用：验证「无界面版命令行控制指令」（**只对内置模拟器测试**）。

真实打印机正在打印，绝不对真机下发暂停/停止指令。

流程：启动模拟器（127.0.0.2）→ 写一份临时配置 → 用子进程调用
``python -m app.headless --control ...`` → 检查模拟器是否收到并响应指令。

用法：``python tools/control_cli_check.py``
"""

from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools._common import enable_utf8, use_temp_config_dir  # noqa: E402

enable_utf8()

# 关键：先把配置目录切到临时目录，绝不能碰用户真实配置
CONFIG_DIR = use_temp_config_dir("bambu-cli-control-")

from app.bambu.models import PrinterInfo, detect_model  # noqa: E402
from app.config import AppConfig, config_path  # noqa: E402
from app.sim.simulator import start_simulator, stop_simulator  # noqa: E402

CODE = "12345678"
ok = True
env = dict(os.environ)
env["BAMBU_MONITOR_CONFIG_DIR"] = CONFIG_DIR
env["PYTHONIOENCODING"] = "utf-8"

print("① 启动 2 台模拟打印机")
printers, responder = start_simulator(2, CODE)
target = printers[0]

print("② 写入临时配置（明文访问代码，仅用于本地测试）")
config = AppConfig()
config.persist = True
for printer in printers:
    config.printers.append(
        PrinterInfo(
            ip=printer.ip,
            serial=printer.serial,
            name=printer.name,
            access_code=CODE,
            model=detect_model(printer.serial, printer.model),
            stream_mode="tcp6000",
        )
    )
print(f"   配置：{config_path()}")
if not os.path.abspath(config_path()).startswith(os.path.abspath(CONFIG_DIR)):
    print("✗ 配置路径不在临时目录，已中止以免覆盖真实配置")
    stop_simulator(printers, responder)
    sys.exit(2)
config.save()


def run_cli(*extra: str) -> tuple[int, str]:
    command = [sys.executable, "-m", "app.headless", *extra]
    result = subprocess.run(
        command,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok
    print(f"   {'✓' if condition else '✗'} {label}{('：' + detail) if detail else ''}")
    if not condition:
        ok = False


print("\n③ --list 读取临时配置")
code, output = run_cli("--list")
check("列出模拟打印机", code == 0 and target.ip in output, (output.strip().splitlines() or [""])[-1])

print("\n④ 安全护栏：stop 必须显式确认")
code, output = run_cli("--control", "stop", "--target", target.ip)
check("未加 --yes 时拒绝执行", code == 2 and "--yes" in output, output.strip().splitlines()[-1] if output else "")
check("模拟器未收到任何指令", not target.received_commands, f"收到 {len(target.received_commands)} 条")

print("\n⑤ 下发控制指令（仍是模拟器）")
for action, value, expect in (
    ("light", "off", ("system", "ledctrl", "off")),
    ("light", "on", ("system", "ledctrl", "on")),
    ("pause", "", ("print", "pause", None)),
    ("resume", "", ("print", "resume", None)),
    ("stop", "", ("print", "stop", None)),
):
    extra = ["--control", action, "--target", target.ip]
    if value:
        extra += ["--value", value]
    if action == "stop":
        extra.append("--yes")
    code, output = run_cli(*extra)
    received = target.received_commands[-1] if target.received_commands else {}
    body = received.get(expect[0], {}) if isinstance(received, dict) else {}
    hit = body.get("command") == expect[1] and (expect[2] is None or body.get("led_mode") == expect[2])
    check(f"{action} {value}".strip(), code == 0 and hit, (output.strip().splitlines() or [""])[-1])

print("\n⑥ 模拟器状态")
print(f"   暂停标记={target.paused} 停止标记={target.stopped} 舱灯={target.light_on} 进度={target.percent}%")
check("停止后进度归零", target.percent == 0)
check("指令全部记录", len(target.received_commands) >= 5, f"{len(target.received_commands)} 条")

stop_simulator(printers, responder)

print("\n" + "=" * 62)
print("无界面控制指令检查：" + ("全部通过 ✓" if ok else "存在失败项 ✗"))
print("=" * 62)
sys.exit(0 if ok else 1)
