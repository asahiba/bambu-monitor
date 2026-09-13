"""把版本号升到新版本（一次性脚本）。

为什么写成脚本而不是手工改：版本号散落在 5 个文件里（Python 包、Gradle、
打包脚本、归集脚本、文档），漏一处的表现是"产物文件名与程序自报版本不一致"，
不跑起来看不出来。这里显式列出每一处，跑完顺带断言没有遗漏。

用法::

    python tools/bump_version.py 1.0.2            # 从当前版本升到 1.0.2
    python tools/bump_version.py 1.0.2 1.0.1      # 显式指定起始版本

起始版本默认从 ``app/__init__.py`` 里读，所以正常情况下只传目标版本即可。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def current_version() -> str:
    """从 app/__init__.py 读当前版本号。"""
    text = (ROOT / "app" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__ = "([^"]+)"', text)
    if not match:
        raise SystemExit("读不出 app/__init__.py 里的 __version__")
    return match.group(1)


def targets(old: str, new: str) -> list[tuple[str, str, str, str]]:
    """(相对路径, 说明, 匹配模式, 替换内容)。

    替换规则用正则，避免误伤文档里的历史引用
    （例如 "基线 v1.0.0"、"compare/v1.0.0"）。
    """
    esc = re.escape(old)
    return [
        ("app/__init__.py", "__version__", rf'__version__ = "{esc}"', f'__version__ = "{new}"'),
        ("android/app/build.gradle", "versionName", rf'versionName "{esc}"', f'versionName "{new}"'),
        (
            "android/app/build.gradle",
            "versionCode",
            r"versionCode (\d+)",
            None,  # 特殊：自增，在下面处理
        ),
        (
            "android/build-apk.ps1",
            "APK 产物名",
            rf"BambuMonitor-{esc}-arm64\.apk",
            f"BambuMonitor-{new}-arm64.apk",
        ),
        (
            "make-bundle.ps1",
            "归集源路径",
            rf"BambuMonitor-{esc}-arm64\.apk",
            f"BambuMonitor-{new}-arm64.apk",
        ),
        (
            "docs/PACKAGING.md",
            "安卓产物名",
            rf"dist-android/BambuMonitor-{esc}-arm64\.apk",
            f"dist-android/BambuMonitor-{new}-arm64.apk",
        ),
        (
            "docs/PACKAGING.md",
            "示例输出",
            rf"BambuMonitor-cli\.exe --version    → Bambu Monitor {esc}",
            f"BambuMonitor-cli.exe --version    → Bambu Monitor {new}",
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


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    new = argv[1]
    old = argv[2] if len(argv) > 2 else current_version()
    if old == new:
        print(f"当前已经是 {new}，无需改动。")
        return 0
    print(f"版本号：{old} -> {new}")

    failures: list[str] = []
    for rel, label, pattern, repl in targets(old, new):
        path = ROOT / rel
        if not path.is_file():
            failures.append(f"{rel} 不存在")
            continue
        raw = path.read_bytes().decode("utf-8-sig")
        if not re.search(pattern, raw):
            failures.append(f"{rel} 里找不到 {label}（模式 {pattern}）—— 可能已经改过或写法变了")
            continue
        if repl is None:
            # versionCode：自增，Android 要求每次发版都涨（否则覆盖安装会被拒）
            raw_bytes = path.read_bytes()
            match = re.search(pattern.encode("utf-8"), raw_bytes)
            assert match is not None
            code = int(match.group(1)) + 1
            count = replace_preserving_bytes(path, pattern, f"versionCode {code}")
            print(f"  [{count} 处] {rel} :: {label} -> {code}")
            continue
        count = replace_preserving_bytes(path, pattern, repl)
        print(f"  [{count} 处] {rel} :: {label}")

    # 顺带确认没有残留（只查会真正影响运行/文件名的地方）
    print()
    leftovers = []
    for rel in (
        "app/__init__.py",
        "android/app/build.gradle",
        "android/build-apk.ps1",
        "make-bundle.ps1",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for match in re.finditer(re.escape(old), text):
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
    raise SystemExit(main(sys.argv))
