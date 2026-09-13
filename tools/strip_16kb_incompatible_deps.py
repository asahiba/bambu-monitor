"""一次性脚本：从安卓构建里去掉 16KB 页不兼容的原生依赖。

## 背景

`opencv-python-headless` / `numpy` / `cryptography` 在 Chaquopy 仓库里的 wheel
是 **4096 字节对齐**的，而 16KB 内存页的设备（新 ARM Chromebook、Pixel 8+ 等）
**拒绝加载**这类库。Python 启动时导入它们会抛 Java 异常 —— 表现为**闪退**。

Chaquopy 17.0.0 的发布说明原文就写了这一点：
"any Android wheels built before October 2024 will still fail to load on 16 KB devices."

这三个依赖在代码里都是**可选的**，都有兜底：
* opencv 缺失 → RTSPS 通道不可用，自动退回 6000 端口 JPEG（app/bambu/rtsp.py）
* numpy 只被 cv2 需要
* cryptography 只给内置模拟器生成自签证书（安卓版不用模拟器）

所以从安卓构建里去掉它们，换取"在所有设备上都能启动"。
RTSPS 画面在安卓上暂时没有（X1/X2D/H2 系列只能用 6000 端口，部分机型不支持）。

用法：python tools/strip_16kb_incompatible_deps.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GRADLE = ROOT / "android" / "app" / "build.gradle"

OLD_PIP = """        pip {
            // 纯 Python 包（从 PyPI 装）
            install "paho-mqtt"
            // 原生包（Chaquopy 仓库里有 android_24_arm64_v8a 的 cp310 wheel）
            install "cryptography"            // 生成自签证书 / 加解密用
            install "opencv-python-headless"  // RTSPS(322) 通道拉流用

            // numpy：android_24 上没有 cp310 的 numpy wheel，只有 android_21 的。
            // 直接给本地 wheel 路径（android/offline-wheels 由 build-apk.ps1 下载），
            // 绕开索引的平台标签匹配。若该文件不存在则退回按名字装，
            // 让 Gradle 明确报错而不是静默少装一个包（缺 numpy 时 cv2 无法导入）。
            def numpyWheel = new File(rootProject.projectDir,
                    "offline-wheels/numpy-1.26.2-0-cp310-cp310-android_21_arm64_v8a.whl")
            if (numpyWheel.exists()) {
                install numpyWheel.getCanonicalPath()
            } else {
                install "numpy"
            }
        }"""

NEW_PIP = """        pip {
            // ⚠️ 只装**纯 Python** 包。
            //
            // 这里刻意不装 opencv-python-headless / numpy / cryptography：
            // Chaquopy 仓库里给这三个的预编译 wheel 是 **4096 字节对齐**的，
            // 而 16KB 内存页的设备（新 ARM Chromebook、Pixel 8+ 等）**拒绝加载**
            // 这类库。Python 启动时导入它们会抛 Java 异常 —— 用户看到的是**闪退**。
            //
            //   Chaquopy 17.0.0 发布说明原文：
            //   "any Android wheels built before October 2024 will still fail to
            //    load on 16 KB devices."
            //
            // 本工程里的这三项都是**可选**的，都有兜底：
            //   * opencv 缺失 -> RTSPS 通道不可用，自动退回 6000 端口 JPEG
            //     （app/bambu/rtsp.py 里 available() 判空并给出说明）
            //   * numpy       -> 只被 cv2 需要
            //   * cryptography-> 只给内置模拟器生成自签证书（安卓版不用模拟器）
            //
            // 代价：X1 / X2D / H2 这类**只提供 RTSPS** 的机型在安卓上看不到画面
            // （A1 / P1 / A2L 走 6000 端口，不受影响）。换取"在所有设备上都能启动"，
            // 这个取舍是划算的 —— 闪退等于完全不能用。
            //
            // 若将来 Chaquopy 提供了 16KB 对齐的 wheel，把下面几行加回来即可。
            install "paho-mqtt"
        }"""


def main() -> int:
    # ⚠️ 踩过的坑（都在这段代码里）：
    #   1. 用 utf-8-sig 读：文件带 BOM，按 utf-8 读会多出 U+FEFF 字符；
    #   2. 换行统一成 \n 再匹配：文件是 CRLF，而模式串是 LF；
    #   3. 别用"整块逐字符相等"来定位 —— 注释里的空白/全角字符差一个就永远匹配不上。
    #      改成按 `pip {` … 对应缩进的 `}` 定位整块替换。
    raw = GRADLE.read_bytes()
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    crlf = b"\r\n" in raw
    text = raw.decode("utf-8-sig").replace("\r\n", "\n")

    if "4096 字节对齐" in text:
        print("已经是精简后的状态，无需改动。")
        return 0

    match = re.search(r"^        pip \{.*?^        \}\n", text, re.S | re.M)
    if match is None:
        print("[失败] 找不到 pip 块", file=sys.stderr)
        return 1

    text = text[: match.start()] + NEW_PIP + "\n" + text[match.end() :]
    if crlf:
        text = text.replace("\n", "\r\n")
    GRADLE.write_bytes(text.encode("utf-8-sig" if had_bom else "utf-8"))
    print("已去掉 opencv-python-headless / numpy / cryptography，只保留 paho-mqtt")
    print(f"（BOM={had_bom}，CRLF={crlf}，均保持原样）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
