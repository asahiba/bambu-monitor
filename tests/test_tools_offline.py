"""把「离线可跑的工具脚本」接进 pytest（CI 会自动跑到）。

## 为什么需要

`tools/` 下的脚本是「边打印边判断 + 退出码表达结果」的诊断工具，绝大多数需要
真机、真端口，不适合进 CI。但其中有一类是**纯离线静态检查**，它们恰恰最容易悄悄
烂掉 —— 因为没人跑：`tools/check_qi_imports_probe.py`（对，就是它）曾经在任何输出
被重定向/管道捕获的场景下直接 ``UnicodeEncodeError`` 崩掉，把「检查通过」报成
「工具自己失败了」；`tools/check_workflow_ps.py` 则因为缺 ``PyYAML`` 一运行就甩
traceback。

本文件做两件事：

1. 把三个离线检查工具当子进程跑一遍，断言**退出码 0**（子进程 + 管道，正是它们
   以前崩掉的姿势）；
2. 静态扫描 ``tools/*.py``：凡是会打印 ``✓`` / ``✗`` / ``⚠`` 之类非 ASCII 的工具，
   必须先调用 `enable_utf8()`。

CI 里（Linux / 无 Qt）需要跳过或自动降级的工具会自行 skip，不会让构建变红。
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS = PROJECT_ROOT / "tools"

#: GBK（Windows 默认控制台编码）里没有的字符：打印它们而不切 UTF-8 就会崩
RISKY_CHARS = re.compile(r"[✓✗⚠→…℃]")

#: 离线、不需要真机、不需要 Qt 的检查工具（跑得也快）
OFFLINE_CHECKS = [
    "check_qt_imports_probe.py",  # 自证 Qt 导入检查器（纯文本分析）
    "check_workflow_ps.py",  # 工作流 pwsh 步骤语法（非 Windows / 缺 PyYAML 会自行跳过）
    "run_bat_selfheal_check.py",  # run.bat 的虚拟环境自愈分支（非 Windows 会自行跳过）
]


def _run_tool(name: str, timeout: int = 180) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = ""  # 故意不给：让工具自己处理编码（这正是要测的）
    env["BAMBU_MONITOR_CONFIG_DIR"] = str(PROJECT_ROOT / ".pytest_tool_config")
    return subprocess.run(
        [sys.executable, str(TOOLS / name)],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        timeout=timeout,
    )


@pytest.mark.parametrize("name", OFFLINE_CHECKS)
def test_离线检查工具能以退出码0结束(name):
    """契约：这些工具必须能「被管道捕获着」跑完，并按退出码表达结果。

    以前 ``check_qt_imports_probe.py`` 在 ``stdout`` 被重定向（CI / ``> log.txt`` /
    ``subprocess(capture_output=True)``）时会 ``UnicodeEncodeError`` 崩掉，
    于是「检查通过」被报成「工具失败」。
    """
    result = _run_tool(name)
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    assert result.returncode == 0, f"{name} 退出码 {result.returncode}：\n{output}"
    assert "Traceback" not in output, f"{name} 抛了异常：\n{output}"


@pytest.mark.parametrize("name", OFFLINE_CHECKS)
def test_离线检查工具有可读结论(name):
    """契约：工具要说明「检查了什么、结论是什么」，而不是默默退出 0。"""
    output = (_run_tool(name).stdout + _run_tool(name).stderr).decode("utf-8", errors="replace")
    assert output.strip(), f"{name} 一点输出都没有，用户无法判断它做了什么"
    assert any(mark in output for mark in ("[OK]", "✓", "skip", "全部通过", "通过")), (
        f"{name} 的输出里看不出结论：\n{output}"
    )


def _printed_strings(tree: ast.AST) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
        if name not in {"print", "write"}:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.append(arg.value)
            elif isinstance(arg, ast.JoinedStr):
                for piece in arg.values:
                    if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                        out.append(piece.value)
    return out


def test_打印风险字符的工具必须切到utf8():
    """回归：Windows 默认编码是 GBK，``✓``/``✗`` 打不出来。

    只对 ``print(...)`` 里的**字面量**做判断（不追踪变量），够用且不会误报：
    真正踩坑的写法就是 ``print("✓ 通过")`` 这种。

    两种写法都算合格：调用 ``_common.enable_utf8()``，或自己写
    ``sys.stdout.reconfigure(encoding="utf-8", ...)``（老脚本里有几处）。
    """
    offenders: list[str] = []
    for path in sorted(TOOLS.glob("*.py")):
        if path.name == "_common.py":  # enable_utf8 自己的家
            continue
        source = path.read_text(encoding="utf-8")
        if "enable_utf8()" in source or "reconfigure(encoding=" in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - 语法错的工具由别的测试抓
            continue
        if any(RISKY_CHARS.search(text) for text in _printed_strings(tree)):
            offenders.append(path.name)
    assert not offenders, (
        "这些工具会打印 ✓/✗ 之类字符却既没调用 enable_utf8() 也没 reconfigure，"
        f"输出被重定向时会崩：{offenders}"
    )


def test_工具脚本都能被解析():
    """契约：tools/*.py 至少得是合法 Python（语法错在 CI 里根本不会暴露）。"""
    broken: list[str] = []
    for path in sorted(TOOLS.glob("*.py")):
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            broken.append(f"{path.name}: {exc}")
    assert not broken, "存在语法错误的工具脚本：\n" + "\n".join(broken)


def test_检查类工具都有退出码():
    """契约（KNOWN_ISSUES #14）：名字里带 ``check`` 的工具必须用退出码表达结果。

    否则它们永远进不了 CI（脚本本身能跑通、结论却看不出来）。
    """
    offenders: list[str] = []
    for path in sorted(TOOLS.glob("*check*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        has_exit = any(
            isinstance(node, ast.Call)
            and (
                (getattr(node.func, "id", "") == "exit")
                or (getattr(node.func, "attr", "") == "exit")
            )
            for node in ast.walk(tree)
        ) or any(isinstance(node, ast.Raise) for node in ast.walk(tree))
        if not has_exit:
            offenders.append(path.name)
    assert not offenders, f"这些检查脚本没有退出码，无法进 CI：{offenders}"


def test_run_bat_检查脚本登记在工具清单里():
    """契约：新增工具必须写进 ``tools/README.md``（那里是唯一的脚本索引）。"""
    readme = (TOOLS / "README.md").read_text(encoding="utf-8")
    for name in OFFLINE_CHECKS:
        assert name in readme, f"{name} 没有登记在 tools/README.md"
