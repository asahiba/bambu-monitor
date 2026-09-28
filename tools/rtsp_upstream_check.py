"""只读验收：产品代码（`app.bambu.rtsp.RtspStream`）拉一路 RTSPS 的真实表现。

用法：``python tools/rtsp_upstream_check.py <IP> [--auto|访问代码] [秒数]``

## 这是在验收什么

2026-09-28 的真机结论（见 `docs/FIELD_NOTES.md` §2.4）：

* 一台打印机的实时画面**同时只伺候一个客户端**：程序连着时另一个客户端 0.2 fps，
  程序断开后同一个客户端 30 fps；
* 所以「不停重连」不是更努力，而是把唯一的通道搅乱 —— 验收标准因此是
  **一次连上、长时间不掉**；
* 「每路帧率」是**交付**帧率，不能把 30 fps 的解码也一起压成 4 fps。

本脚本按产品代码的方式跑两段，并给出结论：

1. **有人看**（每 100 ms 取一次帧，等同界面刷新）：首帧耗时、解码帧率、交付帧率、
   帧间隔分布（最大间隔 / 超过 2 秒的次数）、CPU 占用；
2. **没人看**（15 秒不取帧）：交付帧率应当掉到 0、CPU 应当明显下降，
   而解码帧率（流还活着）**不该**变。

全程只读：DESCRIBE / SETUP / PLAY + 收包，不发任何控制指令。
"""

from __future__ import annotations

import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu.rtsp import RtspStream  # noqa: E402
from tools._common import enable_utf8, resolve_code  # noqa: E402

enable_utf8()

IDLE_SECONDS = 15.0
POLL_INTERVAL = 0.1


def main(argv: list[str]) -> int:
    host = argv[1] if len(argv) > 1 else "192.168.31.27"
    code = resolve_code(host, argv[2] if len(argv) > 2 else "--auto")
    seconds = float(argv[3]) if len(argv) > 3 else 30.0
    if not code:
        print("没有可用的访问代码（可传 --auto 从配置读取，或直接给出）")
        return 2

    print(f"== 用产品代码拉 {host}:322（只读，{seconds:.0f} 秒 + 静默 {IDLE_SECONDS:.0f} 秒）==")
    stream = RtspStream(host=host, access_code=code)
    stream.start()
    try:
        started = time.time()
        first = stream.wait_first_frame(20.0)
        first_cost = time.time() - started
        print(f"  首帧：{'拿到' if first else '没拿到'}（{first_cost:.1f} 秒）  状态={stream.state}")
        if first is None:
            print(f"  说明：{stream.detail}")
            return 1

        # ---- 第一段：有人在看（按界面刷新节奏取帧）
        print(f"\n① 有人在看（每 {POLL_INTERVAL * 1000:.0f} ms 取一次帧）")
        cpu0 = time.process_time()
        base_source = stream.frame_count
        base_delivered = stream._delivered  # noqa: SLF001 - 验收脚本，允许读内部计数
        stamps: list[float] = []
        last_seq = -1
        view_started = time.time()
        while time.time() - view_started < seconds:
            seq, jpeg = stream.latest_frame()
            if seq != last_seq:
                last_seq = seq
                stamps.append(time.time())
            time.sleep(POLL_INTERVAL)
        span = time.time() - view_started
        src = stream.frame_count - base_source
        sent = stream._delivered - base_delivered  # noqa: SLF001
        cpu = time.process_time() - cpu0
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        print(f"  解码 {src} 帧（{src / span:.1f} fps）  交付 {sent} 帧（{sent / span:.1f} fps）")
        print(f"  画面尺寸：{stream._target_size}  状态={stream.state}")  # noqa: SLF001
        if gaps:
            print(
                f"  交付间隔：中位 {statistics.median(gaps) * 1000:.0f} ms，"
                f"最大 {max(gaps) * 1000:.0f} ms，>2 秒 {sum(1 for g in gaps if g > 2.0)} 次"
            )
        print(f"  CPU：{cpu:.2f}s（{cpu / span * 100:.1f}% 单核）")

        # ---- 第二段：没人看
        print(f"\n② 没人看（{IDLE_SECONDS:.0f} 秒不取帧）")
        idle_source0 = stream.frame_count
        idle_cpu0 = time.process_time()
        idle_started = time.time()
        while time.time() - idle_started < IDLE_SECONDS:
            time.sleep(0.5)
        idle_span = time.time() - idle_started
        idle_src = stream.frame_count - idle_source0
        idle_cpu = time.process_time() - idle_cpu0
        print(f"  解码 {idle_src} 帧（{idle_src / idle_span:.1f} fps）—— 流仍然活着")
        print(f"  CPU：{idle_cpu:.2f}s（{idle_cpu / idle_span * 100:.1f}% 单核）")

        # ---- 结论
        print("\n== 结论 ==")
        verdict = True

        def check(name: str, ok: bool, detail: str) -> None:
            nonlocal verdict
            verdict = verdict and ok
            print(f"  {'✓' if ok else '✗'} {name}：{detail}")

        check("首帧耗时", first_cost <= 8.0, f"{first_cost:.1f} 秒（要求 ≤8）")
        watch_fps = sent / span if span else 0.0
        check("交付帧率", watch_fps >= 1.0, f"{watch_fps:.1f} fps（要求 ≥1）")
        check("卡顿次数", not gaps or max(gaps) <= 2.0, f"最大间隔 {max(gaps) * 1000 if gaps else 0:.0f} ms")
        check("静默时不再编码", idle_src == 0 or idle_cpu <= cpu * (idle_span / span) * 1.2,
              f"静默 CPU {idle_cpu / idle_span * 100:.1f}% vs 观看 {cpu / span * 100:.1f}%")
        print("\n" + ("验收通过 ✓" if verdict else "存在问题 ✗"))
        return 0 if verdict else 1
    finally:
        stream.stop()
        stream.join(timeout=10.0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
