"""从 CHANGELOG.md 里抽出某个版本的段落，写成 Release 的发布说明。

用法：``python tools/extract_release_notes.py v1.0.5 release/RELEASE_NOTES.md``

## 为什么需要它

发布工作流原先用 `generate_release_notes: true` 自动生成说明 —— 但项目是**直接推
commit**（不走 PR），GitHub 几乎产不出内容：v1.0.5 第一次发布时发布页上只有一行
`compare/v1.0.4...v1.0.5` 链接，用户看不到"这一版修了什么"。

而 `CHANGELOG.md` 里本来就有人写的、带原因与验证方式的说明。所以发布时**直接取那一段**。

## 为什么是 Python 而不是 shell/awk

同一段抽取逻辑要在**本地也能验证**（awk 在开发机上不一定有，而且写错了只有发版时
才暴露 —— 表现为"发布页空白"，不报错）。Python 两边都有，`tests/test_release_notes.py`
直接调用本模块，改坏了立刻红。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
#: 版本段落的标题形如 `## [1.0.5] — 2026-09-18`（日期与破折号可有可无）
HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\][^\n]*$", re.M)

#: 每个发布页都要有的「下载哪个」与校验方式。
#:
#: 为什么写在这里而不是 CHANGELOG：这是**发布页**的内容（版本段落只讲这一版改了什么），
#: 而 v1.0.5 发布时这段是我手工调 API 补上去的 —— 手工步骤=下次一定会漏。
#: 放在抽取工具里，任何版本发布都会自动带上，且由 tests/test_release_notes.py 锚定。
#: 写法上刻意简短：发布页是给用户看的，不是维护者笔记。
FOOTER = """---

## 下载哪个？

| 你的环境 | 下载 |
| --- | --- |
| Windows，要界面 | `BambuMonitor-windows-x64.exe` |
| Windows，只要命令行 | `BambuMonitor-windows-x64-cli.exe` |
| Linux 桌面 | `BambuMonitor-linux-gui-x64` |
| Linux 服务器 / NAS（无界面） | `BambuMonitor-linux-headless-x64` |
| 群晖 / Unraid 等 Docker | `BambuMonitor-docker-image.tar.gz` |
| 安卓手机 / 平板 | `BambuMonitor-android-arm64.apk` |

下载后建议校验一下：`sha256sum -c SHA256SUMS.txt`（Windows 用
`certutil -hashfile <文件> SHA256`）。

Windows exe 没有代码签名，被杀毒软件误报是常见现象。网页和控制走局域网 HTTP，
不要把端口暴露到公网。
"""


def extract(version: str, changelog: Path | None = None) -> str:
    """返回该版本的段落（含标题），到下一个 `## ` 之前。找不到返回空串。"""
    text = (changelog or CHANGELOG).read_text(encoding="utf-8")
    headings = list(HEADING.finditer(text))
    for index, match in enumerate(headings):
        if match.group("version") != version:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        return text[match.start() : end].strip()
    return ""


def build_body(tag: str, changelog: Path | None = None) -> tuple[str, bool]:
    """拼出发布说明正文（版本段落 + 固定的「下载哪个」页脚）。返回 ``(正文, 是否取到了版本段落)``。"""
    version = tag.lstrip("v")
    section = extract(version, changelog)
    if not section:
        return (
            f"# {tag}\n\n"
            f"> ⚠️ `CHANGELOG.md` 里没有 `[{version}]` 段落，"
            "这一版的说明请看仓库根目录的 CHANGELOG（或下方自动生成的变更列表）。\n\n" + FOOTER,
            False,
        )
    return section + "\n\n" + FOOTER, True


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    tag, out_path = argv[1], argv[2]
    body, found = build_body(tag)
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    if found:
        print(f"已写入发布说明：{out_path}（{len(body)} 字符）")
        print("--- 前 12 行 ---")
        print("\n".join(body.splitlines()[:12]))
        return 0
    print(
        f"::warning::CHANGELOG.md 里没有 [{tag.lstrip('v')}] 的段落，"
        "发布说明退回自动生成的变更列表",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
