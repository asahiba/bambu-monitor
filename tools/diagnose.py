"""一站式诊断某台打印机的遥测与视频通道。

用法：``python tools/diagnose.py <IP> [访问代码|--auto]``
（``--auto`` 表示从 %APPDATA%\\BambuMonitor\\config.json 读取该 IP 已保存的访问代码）

流程实现在 `app/bambu/diagnostics.py`，与界面上的「通道诊断」对话框**共用同一份** ——
以前这里是另一份实现，连 RTSPS 的候选路径都只有界面版没有命令行版（于是出现过
「命令行说通了、界面说不行」）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu import diagnostics  # noqa: E402
from app.bambu.models import detect_model  # noqa: E402
from tools._common import enable_utf8, lookup_serial, resolve_code  # noqa: E402

enable_utf8()


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    host = argv[0]
    code = resolve_code(host, argv[1] if len(argv) > 1 else "")
    serial = lookup_serial(host)
    if not code:
        print("✗ 没有可用的访问代码（--auto 也读不到），请先给出或先关闭一次软件以保存配置")
        return 2

    print("=" * 70)
    print(f"诊断 {host}（访问代码 {len(code)} 位，序列号 {serial or '未知'}）")
    print("=" * 70)

    for event in diagnostics.run(host, code, serial=serial, with_rtsp_frame=True):
        if isinstance(event, diagnostics.SectionStart):
            print(f"\n{event.title}")
        else:
            for line in event.lines:
                print(line)

    print(f"\n机型推断：{detect_model(serial)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
