"""把版本号从 1.0.0 升到 1.0.1（一次性脚本，跑完即可删）。

为什么写成脚本而不是手工改：版本号散落在 5 个文件里（Python 包、Gradle、
打包脚本、归集脚本、文档），漏一处的表现是"产物文件名与程序自报版本不一致"，
不跑起来看不出来。这里显式列出每一处，跑完顺带断言没有遗漏。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

OLD, NEW = "1.0.0", "1.0.1"
ROOT = Path(__file__).resolve().parents[1]

#: (相对路径, 说明, 替换规则)
#: 替换规则用正则，避免误伤文档里的历史引用（例如 "基线 v1.0.0"、"compare/v1.0.0"）
TARGETS = [
    ("app/__init__.py", "__version__", r'__version__ = "1\.0\.0"', f'__version__ = "{NEW}"'),
    ("android/app/build.gradle", "versionName", r'versionName "1\.0\.0"', f'versionName "{NEW}"'),
    ("android/app/build.gradle", "versionCode", r"versionCode 1\b", "versionCode 2"),
    (
        "android/build-apk.ps1",
        "APK 产物名",
        r"BambuMonitor-1\.0\.0-arm64\.apk",
        f"BambuMonitor-{NEW}-arm64.apk",
    ),
    ("make-bundle.ps1", "归集源路径", r"BambuMonitor-1\.0\.0-arm64\.apk", f"BambuMonitor-{NEW}-arm64.apk"),
    (
        "docs/PACKAGING.md",
        "安卓产物名",
        r"dist-android/BambuMonitor-1\.0\.0-arm64\.apk",
        f"dist-android/BambuMonitor-{NEW}-arm64.apk",
    ),
    (
        "docs/PACKAGING.md",
        "示例输出",
        r"BambuMonitor-cli\.exe --version    → Bambu Monitor 1\.0\.0",
        f"BambuMonitor-cli.exe --version    → Bambu Monitor {NEW}",
    ),
]


def replace_preserving_bytes(path: Path, pattern: str, repl: str) -> int:
    """按**字节**做替换，保证换行（CRLF/LF）与 BOM 原样不动。

    踩过的坑：一开始用 ``path.write_text(newline="")`` 写回，把 `.ps1` 的
    CRLF 全变成了 LF —— 而含 here-string（``@"`` … ``"@``）的 `.ps1`
    在 PowerShell 5.1 下会因此报
    ``The string is missing the terminator: "@``。版本号没写错，脚本先坏了。

    这里改成对原始字节做正则替换，从根上不会碰到换行。

    模式与替换串统一按 **UTF-8** 编码：文件本身就是 UTF-8，而 UTF-8 是
    自同步编码（多字节序列的每个字节都 >= 0x80），按字节替换不会切坏别的字符。
    （一开始写成 ``pattern.encode("ascii")``，遇到含 ``→`` 的模式直接抛
    ``UnicodeEncodeError``。）
    """
    raw = path.read_bytes()
    pattern_bytes = pattern.encode("utf-8")
    repl_bytes = repl.encode("utf-8")
    new_raw, count = re.subn(pattern_bytes, repl_bytes, raw)
    if count:
        path.write_bytes(new_raw)
    return count


def main() -> int:
    failures: list[str] = []
    for rel, label, pattern, repl in TARGETS:
        path = ROOT / rel
        if not path.is_file():
            failures.append(f"{rel} 不存在")
            continue
        raw = path.read_bytes().decode("utf-8-sig")
        if not re.search(pattern, raw):
            failures.append(f"{rel} 里找不到 {label}（模式 {pattern}）—— 可能已经改过或写法变了")
            continue
        count = replace_preserving_bytes(path, pattern, repl)
        print(f"  [{count} 处] {rel} :: {label}")

    # 顺带确认没有残留（只查会真正影响运行/文件名的地方）
    print()
    leftovers = []
    for rel in ("app/__init__.py", "android/app/build.gradle", "android/build-apk.ps1",
                "make-bundle.ps1"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for match in re.finditer(r'"?1\.0\.0"?', text):
            line = text[: match.start()].count("\n") + 1
            body = text.splitlines()[line - 1].strip()
            # 注释里提到历史版本是允许的
            if not body.startswith("#") and "基线" not in body:
                leftovers.append(f"{rel}:{line}: {body}")
    if leftovers:
        failures.append("仍有未更新的版本号：\n    " + "\n    ".join(leftovers))

    if failures:
        print("\n[失败]", file=sys.stderr)
        for item in failures:
            print("  - " + item, file=sys.stderr)
        return 1
    print("\n全部更新完毕，无遗漏。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
