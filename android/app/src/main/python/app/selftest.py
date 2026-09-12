"""无界面端到端自检：自动搜索 → MQTT 遥测 → 6000 端口画面。

用法：``python -m app --core-test``
使用内置模拟器，不需要真实打印机。
"""

from __future__ import annotations

import os
import sys
import time

from .bambu.discovery import discover
from .bambu.ports import DEFAULT_ACCESS_CODE
from .bambu.probe import probe_printer
from .sim.simulator import SIM_DIR, start_simulator, stop_simulator

ACCESS_CODE = DEFAULT_ACCESS_CODE


def run_core_test() -> int:
    print("=" * 68)
    print("拓竹监控台 · 核心链路自检")
    print("=" * 68)

    printers, responder = start_simulator(3, ACCESS_CODE)
    ok = True
    try:
        print("\n[1/3] 启动模拟打印机…")
        for printer in printers:
            print(f"      {printer.ip:<12} {printer.model:<8} {printer.serial}  {printer.name}")
        time.sleep(1.0)

        print("\n[2/3] 自动搜索（SSDP + 2021 广播，8 秒）…")
        found = discover(timeout=8.0)
        for info in found:
            print(f"      发现 {info.ip:<12} {info.model.label:<8} {info.serial}  {info.name}")
        if not found:
            print("      ✗ 未发现任何设备")
            ok = False

        print("\n[3/3] 连接首台设备（遥测 + 画面）…")
        # 只挑模拟器（127.0.0.x），避免误连局域网里的真实打印机
        sim_ips = {printer.ip for printer in printers}
        target = next((info for info in found if info.ip in sim_ips), None)
        if target is None:
            target = next((info for info in found if info.ip.startswith("127.")), None)
        if target is None:
            from .bambu.models import PrinterInfo

            print("      未在搜索结果里找到模拟打印机，改用直连方式测试")
            target = PrinterInfo(
                ip=printers[0].ip,
                serial=printers[0].serial,
                name=printers[0].name,
                access_code=ACCESS_CODE,
            )
        result = probe_printer(
            target.ip,
            ACCESS_CODE,
            serial=target.serial,
            timeout=10.0,
            on_step=lambda text: print(f"      · {text}"),
        )
        print("\n" + result.summary())
        if not result.mqtt_ok:
            ok = False
            print("      ✗ 遥测未通过")
        if not result.camera_ok:
            ok = False
            print("      ✗ 画面未通过")
        if result.frame:
            os.makedirs(SIM_DIR, exist_ok=True)
            frame_path = os.path.join(SIM_DIR, "frame.jpg")
            with open(frame_path, "wb") as handle:
                handle.write(result.frame)
            print(f"      已保存一帧画面：{frame_path}（{len(result.frame) // 1024} KB）")
        if result.status is not None:
            status = result.status
            print(
                f"      进度 {status.progress}% · 剩余 {status.remaining_text} · "
                f"喷嘴 {status.nozzle_temper:.1f}/{status.nozzle_target_temper:.0f}℃ · "
                f"热床 {status.bed_temper:.1f}/{status.bed_target_temper:.0f}℃"
            )
    finally:
        stop_simulator(printers, responder)

    print("\n" + "=" * 68)
    print("自检结果：" + ("全部通过 ✓" if ok else "存在失败项 ✗"))
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run_core_test())
