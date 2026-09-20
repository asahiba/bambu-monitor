"""发布说明抽取的契约测试（`tools/extract_release_notes.py`）。

## 为什么单独立一条

发布工作流会把 `CHANGELOG.md` 里对应版本的段落写成 Release 页面的说明。
这段逻辑一旦出问题，**只在发版时暴露**，而且表现为"发布页没有说明"（不报错）：
v1.0.5 第一次发布就是这样 —— `generate_release_notes` 在直接推 commit（无 PR）的
仓库里几乎产不出内容，发布页上只有一行 compare 链接。

所以这里在本地就把三件事钉死：

* 当前版本（`app.__version__`）在 CHANGELOG 里**必须有**段落，且抽得干净；
* 段落不能把下一个版本的内容带进来；
* 找不到时给兜底说明 + 提前警告，而不是写一个空文件。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "extract_release_notes.py"


def _tool():
    spec = importlib.util.spec_from_file_location("extract_release_notes", TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_当前版本在CHANGELOG里有段落且抽得干净():
    from app import __version__

    module = _tool()
    section = module.extract(__version__)
    assert section, f"CHANGELOG.md 里没有 [{__version__}] 段落 —— 发版前必须补上"
    assert section.lstrip().startswith(f"## [{__version__}]"), section[:60]
    # 只应该有这一个版本标题（不能把下一个版本的段落带进来）
    headings = [line for line in section.splitlines() if line.startswith("## [")]
    assert headings == [headings[0]], f"段落里混入了其它版本：{headings}"
    assert len(section.splitlines()) >= 5, "段落内容过少，可能抽取范围不对"


def test_抽取历史版本也正确():
    module = _tool()
    for version in ("1.0.4", "1.0.3", "1.0.0"):
        section = module.extract(version)
        assert section.startswith(f"## [{version}]"), f"{version} 段落开头不对：{section[:40]}"
        assert f"[{version}]" in section.splitlines()[0]


def test_找不到版本时给兜底说明而不是空文件():
    module = _tool()
    body, found = module.build_body("v9.9.9")
    assert found is False
    assert body.strip(), "兜底说明不能是空的（发布页会一片空白）"
    assert "9.9.9" in body
    assert "CHANGELOG" in body, "要告诉用户去哪儿看完整说明"


def test_发布说明总是带上下载哪个这份页脚():
    """契约：每个发布页都要有「下载哪个」表与校验方式。

    v1.0.5 发布时这段是**手工调 API 补**上去的 —— 手工步骤一定会漏。
    现在放进抽取工具：任何版本（含找不到段落时的兜底）都自动带上。
    """
    module = _tool()
    from app import __version__

    for tag in (f"v{__version__}", "v9.9.9"):
        body, _found = module.build_body(tag)
        assert "## 下载哪个？" in body, f"{tag} 的发布说明缺「下载哪个」"
        for name in (
            "BambuMonitor-windows-x64.exe",
            "BambuMonitor-linux-headless-x64",
            "BambuMonitor-docker-image.tar.gz",
            "BambuMonitor-android-arm64.apk",
            "SHA256SUMS.txt",
        ):
            assert name in body, f"{tag} 的发布说明里没有提到 {name}"
        assert "不要" in body and "公网" in body, "要提醒用户别把端口暴露到公网"


def test_命令行会写出文件并返回0(tmp_path, capsys):
    module = _tool()
    from app import __version__

    target = tmp_path / "RELEASE_NOTES.md"
    assert module.main(["extract_release_notes.py", f"v{__version__}", str(target)]) == 0
    assert target.is_file()
    assert f"[{__version__}]" in target.read_text(encoding="utf-8")


def test_参数不足时返回2():
    module = _tool()
    assert module.main(["extract_release_notes.py"]) == 2


def test_工作流用的是这个脚本而不是另写一段():
    """契约：发布流程必须调用本工具（避免"两处各写一份抽取逻辑"再次漂移）。"""
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "tools/extract_release_notes.py" in workflow
    assert "awk" not in workflow.split("生成发布说明")[1].split("建 Release")[0], (
        "发布说明的抽取不该另写一段 awk（本地无法验证）"
    )
