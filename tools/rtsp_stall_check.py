"""测量真实 X2D 的 RTSPS 拉流稳定性（只读，不下发任何控制指令）。

用法::

    python tools/rtsp_stall_check.py 192.168.31.110 --auto --seconds 30

它做的事：

1. 用**与 `app/bambu/rtsp.py` 完全相同的方式**打开 RTSPS（同一组 FFmpeg 选项）；
2. 逐帧记录「两次取到帧之间的间隔」，最后打印：帧数、fps、最大间隔、超过指定阈值的次数；
3. 顺便打印 OpenCV/FFmpeg 的版本与本次实际生效的 `OPENCV_FFMPEG_CAPTURE_OPTIONS`。

为什么要单独量这个：用户反馈"帧数高的机型（X2D）画面经常卡住"，而卡住有两种完全不同的
原因 —— ①流本身断开（会走重连逻辑，能自愈）②**socket 半死不活，`read()` 一直不返回**
（线程卡死在 FFmpeg 里，谁都救不回来）。第 ② 种在逐帧间隔上看得很清楚：
正常是 100~200 ms，卡住时会出现几秒甚至几十秒的空档。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu.ports import RTSP_PORT  # noqa: E402
from tools._common import enable_utf8, resolve_code  # noqa: E402


def _via_stream(host: str, code: str, seconds: float, stall: float) -> int:
    """用产品代码那条路测：RtspStream（开流前的 TCP 探测 + 退避重连）。

    这是"修完之后到底好不好用"的判据 —— 裸 cv2 那一段只说明打印机本身爱不爱理人。
    """
    from app.bambu.rtsp import RtspStream

    states: list[tuple[float, str, str]] = []
    started = time.time()

    def on_state(state: str, detail: str) -> None:
        states.append((time.time() - started, state, detail))

    stream = RtspStream(host=host, access_code=code, on_state=on_state, max_fps=10.0)
    stream.start()
    print(f"用产品代码（RtspStream）连 {host}，最多等 60 秒出首帧…")
    first = stream.wait_first_frame(timeout=60.0)
    if first is None:
        print("[失败] 60 秒内没出首帧；状态轨迹（前 12 条）：")
        for stamp, state, detail in states[:12]:
            print(f"  {stamp:6.1f}s  {state:10} {detail}")
        stream.stop()
        stream.join(timeout=3.0)
        return 1
    first_after = time.time() - started
    print(f"✓ 首帧用时 {first_after:.1f} 秒（{len(first) / 1024:.0f} KB）")

    frames_before = stream.frame_count
    gaps: list[float] = []
    last = time.time()
    deadline = last + max(3.0, seconds)
    while time.time() < deadline:
        time.sleep(0.1)
        count = stream.frame_count
        if count != frames_before:
            now = time.time()
            gaps.append(now - last)
            last = now
            frames_before = count
    elapsed = time.time() - last if last else 0
    stream.stop()
    stream.join(timeout=3.0)

    took = time.time() - started
    frames = stream.frame_count
    print(f"\n共 {frames} 帧 / {took - first_after:.1f} 秒 = {frames / max(0.1, took - first_after):.2f} fps")
    if gaps:
        worst = max(gaps)
        stalls = [gap for gap in gaps if gap >= stall]
        print(f"  平均间隔 {sum(gaps) / len(gaps) * 1000:.0f} ms，最大 {worst * 1000:.0f} ms，"
              f"卡顿（≥{stall:.0f}s）{len(stalls)} 次")
    retries = [item for item in states if item[1] == RtspStream.STATE_RETRYING]
    print(f"  重连次数：{len(retries)}")
    for stamp, _state, detail in retries[:6]:
        print(f"    {stamp:6.1f}s  {detail}")
    print(f"  最后状态：{stream.state} —— {stream.detail}")
    return 0


def main(argv: list[str] | None = None) -> int:
    enable_utf8()
    parser = argparse.ArgumentParser(description="测量 RTSPS 拉流稳定性（只读）")
    parser.add_argument("host")
    parser.add_argument("--code", default="--auto", help="访问代码；默认从配置里读（--auto）")
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--stall", type=float, default=2.0, help="超过这个间隔算一次卡顿")
    parser.add_argument(
        "--via-stream",
        action="store_true",
        help="用产品代码（RtspStream，含开流前的 TCP 端口探测）而不是裸 cv2 来测",
    )
    args = parser.parse_args(argv)

    code = resolve_code(args.host, args.code)
    if not code:
        print("没有可用的访问代码（可传 --auto 从配置里读）")
        return 2

    if args.via_stream:
        return _via_stream(args.host, code, args.seconds, args.stall)

    # 先按 rtsp.py 的方式设置 FFmpeg 选项（导入 app.bambu.rtsp 就会设置）
    import app.bambu.rtsp as rtsp_module  # noqa: F401  （import 副作用就是设置环境变量）

    print("OPENCV_FFMPEG_CAPTURE_OPTIONS =", os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS"))

    import cv2

    print(f"OpenCV {cv2.__version__}")
    try:
        info = cv2.getBuildInformation()
        for line in info.splitlines():
            if "FFMPEG" in line and ":" in line:
                print("  ", line.strip())
    except Exception:  # noqa: BLE001
        pass

    from app.bambu.rtsp import RtspStream
    from app.bambu.timeouts import RTSP_OPEN_TIMEOUT_MS

    url = RtspStream(host=args.host, access_code=code).url(path="/streaming/live/1")
    safe_url = url.replace(code, "****")
    print(f"\n打开 {safe_url}（超时 {RTSP_OPEN_TIMEOUT_MS} ms）")
    params = [
        int(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC),
        int(RTSP_OPEN_TIMEOUT_MS),
        int(cv2.CAP_PROP_READ_TIMEOUT_MSEC),
        int(RTSP_OPEN_TIMEOUT_MS),
    ]
    started = time.time()
    try:
        capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)
    except TypeError:
        capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    opened_after = time.time() - started
    print(f"isOpened={capture.isOpened()}（耗时 {opened_after:.1f} 秒）")
    if not capture.isOpened():
        print("[失败] 打不开 RTSPS：检查打印机上是否开启「局域网实时画面」，以及访问代码")
        return 1
    try:
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:  # noqa: BLE001
        pass

    frames = 0
    gaps: list[float] = []
    last = time.time()
    deadline = last + max(3.0, args.seconds)
    worst = 0.0
    try:
        while time.time() < deadline:
            before = time.time()
            ok, frame = capture.read()
            after = time.time()
            if not ok or frame is None:
                print(f"  [{after - last:5.2f}s] read 返回失败（流断了）")
                break
            frames += 1
            gap = after - last
            gaps.append(gap)
            last = after
            worst = max(worst, gap)
            if gap >= args.stall:
                print(f"  ⚠️ 第 {frames} 帧前卡了 {gap:.1f} 秒（read 本身耗时 {after - before:.2f}s）")
            elif frames <= 3:
                print(f"  第 {frames} 帧：{frame.shape[1]}×{frame.shape[0]}，间隔 {gap:.2f}s")
    finally:
        capture.release()

    elapsed = time.time() - started
    print(f"\n共 {frames} 帧 / {elapsed:.1f} 秒 = {frames / elapsed:.2f} fps")
    if gaps:
        average = sum(gaps) / len(gaps)
        stalls = [gap for gap in gaps if gap >= args.stall]
        print(f"  平均间隔 {average * 1000:.0f} ms，最大 {worst * 1000:.0f} ms，"
              f"卡顿（≥{args.stall:.0f}s）{len(stalls)} 次")
        print(f"  {'✓ 这段时间里没有卡顿' if not stalls else '⚠️ 出现了卡顿，见上面标记'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
