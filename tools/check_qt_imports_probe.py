"""开发用：自证 tools/check_qt_imports.py 能抓到「从错误模块导入」的问题。

用法：``python tools/check_qt_imports_probe.py``
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import enable_utf8  # noqa: E402

#: 必须先切 stdout 到 UTF-8：本脚本要打印 ✓/✗，而 Windows 默认 GBK 控制台
#: （或把输出重定向到管道/文件时）会直接 UnicodeEncodeError 崩掉 ——
#: 明明是「检查通过」，却被报成工具自身失败。
enable_utf8()

# 自证需要 PySide6：没有它，检查器连"正确的导入"都无法验证（拿不到真实属性名），
# 于是「错误导入必须被查出」那个用例必然失败 —— 那不是工具坏了，而是环境不适用。
# CI 刻意不装 PySide6，所以这里明确跳过并说明原因，别让它报成失败。
try:
    _HAS_PYSIDE = importlib.util.find_spec("PySide6") is not None
except (ImportError, ValueError, AttributeError):  # 父包缺失时 find_spec 会抛
    _HAS_PYSIDE = False
if not _HAS_PYSIDE:
    print("[skip] 未安装 PySide6，本自证无法进行（CI 环境如此）。")
    print("       开发机上请先 pip install -r requirements.txt 再跑。")
    sys.exit(0)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKER = os.path.join(ROOT, "tools", "check_qt_imports.py")

CASES = [
    ("from PySide6.QtWidgets import QFileDialog, QStandardPaths\n", 1, "错误导入必须被查出"),
    (
        "from PySide6.QtWidgets import QFileDialog\nfrom PySide6.QtCore import QStandardPaths\n",
        0,
        "正确导入必须通过",
    ),
]

ok = True
for source, expect_fail, label in CASES:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
        handle.write(source)
        path = handle.name
    try:
        result = subprocess.run(
            [sys.executable, CHECKER, path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        failed = result.returncode != 0
        good = failed == bool(expect_fail)
        print(f"{'✓' if good else '✗'} {label}（退出码 {result.returncode}）")
        if not good:
            ok = False
            print(result.stdout[-600:])
        elif expect_fail:
            hint = [line for line in result.stdout.splitlines() if "正确写法" in line]
            print(f"    {hint[0].strip() if hint else '（未给出正确写法提示）'}")
    finally:
        os.unlink(path)

print("\n" + "=" * 58)
print("Qt 导入检查工具自证：" + ("通过 ✓" if ok else "失败 ✗"))
print("=" * 58)
sys.exit(0 if ok else 1)
