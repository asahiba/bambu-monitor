"""契约：仓库里所有 PySide6 导入都必须「从正确的模块」导入。

``from PySide6.QtWidgets import QStandardPaths`` 这类错误：

* pyflakes / ruff 都查不出来（模块存在，只是名字不在里面）；
* 平时也发现不了，只有点到那个功能才会抛 ``ImportError``；
* 打包成 exe 后报错信息变成
  ``cannot import name 'QStandardPaths' from 'PySide6.QtWidgets' (...MEI0000...\\PySide6\\QtWidgets.pyd)``。

这里直接复用 ``tools/check_qt_imports.py`` 的扫描逻辑，把整仓库过一遍，
让这类问题在 CI 阶段就暴露。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHECKER = PROJECT_ROOT / "tools" / "check_qt_imports.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_qt_imports", CHECKER)
    assert spec and spec.loader, f"无法加载 {CHECKER}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_qt_imports"] = module
    spec.loader.exec_module(module)
    return module


def test_all_pyside_imports_are_valid(capsys):
    """逐个校验 PySide6 导入的属性是否真实存在于该模块。"""
    checker = _load_checker()
    exit_code = checker.main(["app", "tools", "tests", "run_app.py", "run_headless.py"])
    output = capsys.readouterr().out
    assert exit_code == 0, f"存在错误的 PySide6 导入：\n{output}"


def test_checker_detects_wrong_module(tmp_path, capsys):
    """自证：故意写错的导入必须被查出，并给出正确写法。"""
    checker = _load_checker()
    bad = tmp_path / "bad_import.py"
    bad.write_text("from PySide6.QtWidgets import QFileDialog, QStandardPaths\n", encoding="utf-8")
    assert checker.main([str(bad)]) == 1
    output = capsys.readouterr().out
    assert "QStandardPaths" in output
    assert "QtCore" in output, "应当提示正确的来源模块"


def test_checker_accepts_correct_module(tmp_path):
    """自证：正确的导入必须通过。"""
    checker = _load_checker()
    good = tmp_path / "good_import.py"
    good.write_text(
        "from PySide6.QtCore import QStandardPaths\nfrom PySide6.QtWidgets import QFileDialog\n",
        encoding="utf-8",
    )
    assert checker.main([str(good)]) == 0
