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
    """拼出发布说明正文。返回 ``(正文, 是否取到了版本段落)``。"""
    version = tag.lstrip("v")
    section = extract(version, changelog)
    if not section:
        return (
            f"# {tag}\n\n"
            f"> ⚠️ `CHANGELOG.md` 里没有 `[{version}]` 段落，"
            "这一版的说明请看仓库根目录的 CHANGELOG（或下方自动生成的变更列表）。\n",
            False,
        )
    return section + "\n", True


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
