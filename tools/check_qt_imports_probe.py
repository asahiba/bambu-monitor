"""开发用：自证 tools/check_qt_imports.py 能抓到「从错误模块导入」的问题。

用法：``python tools/check_qt_imports_probe.py``
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

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
