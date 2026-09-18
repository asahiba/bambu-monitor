"""静态检查：PySide6 的导入是否「从正确的模块」导入。

这类错误不仅 pyflakes/ruff 查不出来（模块真实存在，只是名字不在里面），
而且平时也发现不了 —— 只有点开那个功能才会抛 ImportError。典型例子：

    from PySide6.QtWidgets import QStandardPaths   # ✗ QStandardPaths 在 QtCore 里

打包成 exe 后报错信息还会变成
``cannot import name 'QStandardPaths' from 'PySide6.QtWidgets' (...MEI0000...\\PySide6\\QtWidgets.pyd)``，
``MEI0000...`` 正是 PyInstaller 解包目录。

用法：``python tools/check_qt_imports.py [目录...]``
"""

from __future__ import annotations

import ast
import importlib
import os
import sys
from typing import Iterable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import enable_utf8  # noqa: E402

#: 必须先切 stdout 到 UTF-8：下面要打印 ✓/✗，而 Windows 默认 GBK 控制台
#: （或输出被管道/文件捕获时）会直接 UnicodeEncodeError 崩掉 ——
#: 那是「检查通过」被报成「工具自己失败」的那类假故障。
enable_utf8()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIRS = ("app", "tools", "tests", "run_app.py", "run_headless.py")

#: 允许被导入的 PySide6 顶层模块（其余名字一律视为可疑）
PYSIDE_PREFIX = "PySide6"


def _display(path: str) -> str:
    """相对仓库显示路径；跨盘符（如临时目录在 C:）时退回绝对路径。"""
    try:
        return os.path.relpath(path, ROOT)
    except ValueError:
        return path


def iter_python_files(targets: Iterable[str]) -> list[str]:
    files: list[str] = []
    for target in targets:
        path = target if os.path.isabs(target) else os.path.join(ROOT, target)
        if os.path.isfile(path) and path.endswith(".py"):
            files.append(path)
        elif os.path.isdir(path):
            for current, _dirs, names in os.walk(path):
                if "__pycache__" in current:
                    continue
                for name in names:
                    if name.endswith(".py"):
                        files.append(os.path.join(current, name))
    return sorted(files)


def module_names(path: str) -> set[str]:
    """返回某个 PySide6 子模块里真实存在的名字。"""
    try:
        module = importlib.import_module(path)
    except Exception as exc:  # noqa: BLE001
        print(f"  跳过 {path}（无法导入：{exc}）")
        return set()
    return set(dir(module))


def main(argv: list[str]) -> int:
    # 没有 PySide6 时这个工具**什么也校验不了**（属性名要靠真实导入才拿得到）。
    # 早退并明确说出来，而不是逐文件打印"跳过"后报告"全部通过" ——
    # 后者会让人以为 CI 真的查过了。
    try:
        importlib.import_module("PySide6")
    except Exception as exc:  # noqa: BLE001
        print(f"[skip] 未安装 PySide6（{exc.__class__.__name__}），无法校验导入的属性名。")
        print("       CI 环境刻意不装 PySide6；开发机上请 pip install -r requirements.txt 后再跑。")
        return 0

    targets = argv or list(DEFAULT_DIRS)
    files = iter_python_files(targets)
    print(f"扫描 {len(files)} 个 Python 文件…\n")

    cache: dict[str, set[str]] = {}
    # 预加载常用子模块：这样即使仓库里只写错了一处，也能给出「应该在哪个模块」
    for name in (
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtNetwork",
        "PySide6.QtSvg",
    ):
        cache[name] = module_names(name)
    problems: list[tuple[str, int, str, str]] = []
    parse_errors: list[tuple[str, str]] = []
    checked = 0

    for file in files:
        try:
            source = open(file, encoding="utf-8").read()
            tree = ast.parse(source)
        except (OSError, SyntaxError) as exc:
            # 解析失败必须算失败，否则工具会「假装通过」
            parse_errors.append((file, str(exc)))
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith(PYSIDE_PREFIX):
                continue
            if node.module not in cache:
                cache[node.module] = module_names(node.module)
            available = cache[node.module]
            if not available:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                checked += 1
                if alias.name not in available:
                    problems.append((file, node.lineno, node.module, alias.name))

    for file, message in parse_errors:
        print(f"✗ {_display(file)} 无法解析：{message}")

    for file, lineno, module, name in problems:
        print(f"✗ {_display(file)}:{lineno}  {module} 里没有 {name}")
        # 指出正确的来源
        for candidate in sorted(cache):
            if candidate == module:
                continue
            if name in cache[candidate]:
                print(f"    → 正确写法：from {candidate} import {name}")
                break

    failed = bool(problems or parse_errors)
    print(
        f"\n共检查 {checked} 条 PySide6 导入，属性错误 {len(problems)} 处，"
        f"文件解析失败 {len(parse_errors)} 个"
    )
    if failed:
        print("=" * 62)
        print("PySide6 导入检查：存在错误 ✗")
        print("=" * 62)
        return 1
    print("=" * 62)
    print("PySide6 导入检查：全部通过 ✓")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
