"""一次性脚本：把 build-apk.ps1 里"下载 numpy wheel"的步骤去掉。

安卓构建已不再安装 opencv / numpy / cryptography（它们 4096 字节对齐，
在 16 KB 页设备上会闪退，见 tools/strip_16kb_incompatible_deps.py），
所以那个预先下载 numpy wheel 的步骤已经没有意义 —— 留着只是白等一次网络请求，
还会让人误以为构建仍然需要它。

用法：python tools/drop_numpy_wheel_step.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "android" / "build-apk.ps1"

REPLACEMENT = """Write-Host ""
Write-Host "=== 3/5 Gradle 构建（首次要下载 Chaquopy/AGP 依赖，约 3-10 分钟）===" -ForegroundColor Cyan"""

HEADER_OLD = """# 为什么不能直接用项目主 .venv（3.13）：Chaquopy 仓库在 android_24_arm64_v8a
# 这个平台标签上，opencv 只有 cp310、numpy 只有 cp313，两者不可能同时满足；
# 而 RTSPS 画面要 opencv，cv2 又硬依赖 numpy，所以只能选 3.10 并把 numpy
# 用本地 wheel 补上（见 app/build.gradle 的 pip 段）。
# Chaquopy 17 起还要求 buildPython 的主次版本与 chaquopy.version 完全一致。"""

HEADER_NEW = """# 为什么不能直接用项目主 .venv（3.13）：Chaquopy 的 install 只接受与它自己
# 平台标签匹配的 wheel，而本工程**刻意不装任何预编译原生包**
# （opencv / numpy / cryptography 在 Chaquopy 仓库里是 4096 字节对齐的，
# 在 16 KB 内存页设备上会拒绝加载并闪退 —— 见
# tools/strip_16kb_incompatible_deps.py 的说明）。
# 于是这里只剩纯 Python 的 paho-mqtt，任何 Python 版本都能装；
# 仍固定 3.10 是因为它是 Chaquopy 17 的默认版本、覆盖最全，
# 且与"曾经用过 cp310 的 opencv"保持一致，避免以后再踩平台标签的坑。
# Chaquopy 17 起要求 buildPython 的主次版本与 chaquopy.version 完全一致。"""


def main() -> int:
    raw = SCRIPT.read_bytes()
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    crlf = b"\r\n" in raw
    text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    failures: list[str] = []

    # 1) 去掉整个"准备 numpy 本地 wheel"段落（从标题到下一个 === 步骤标题之前）
    pattern = re.compile(
        r"\n# --- numpy 的本地 wheel ---\n.*?(?=\nWrite-Host \"\"\nWrite-Host \"=== 3/5)",
        re.S,
    )
    if pattern.search(text):
        text = pattern.sub("\n", text, count=1)
        print("  已删除 numpy wheel 下载段落")
    elif "numpy wheel 下载失败" in text:
        failures.append("找到 numpy wheel 相关代码但没能整段删除")
    else:
        print("  （已经没有该段落，跳过）")

    # 2) 更新文件头注释里的理由
    if HEADER_OLD in text:
        text = text.replace(HEADER_OLD, HEADER_NEW)
        print("  已更新文件头注释")
    elif "刻意不装任何预编译原生包" in text:
        print("  （文件头已是新说明，跳过）")
    else:
        failures.append("文件头注释没匹配到（可能已改过）")

    if failures:
        for item in failures:
            print("  [警告] " + item, file=sys.stderr)

    if crlf:
        text = text.replace("\n", "\r\n")
    SCRIPT.write_bytes(text.encode("utf-8-sig" if had_bom else "utf-8"))
    print(f"已写回（BOM={had_bom}，CRLF={crlf}）")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
