"""检查 `run.bat` 的「虚拟环境自愈」分支真的会触发（Windows 专用）。

用法：``python tools/run_bat_selfheal_check.py``（非 Windows 上直接跳过并退出 0）

## 为什么需要它

`run.bat` 原来只判断 ``.venv\\Scripts\\python.exe`` **是否存在**。本项目历史上
真的踩过这个坑：从 ``D:\\DSH`` 搬到 ``L:\\DSH``、基础 Python 又被删掉之后，
那个 python.exe 文件还在、但一启动就退出（退出码 103，报
``did not find executable at ...``），于是 run.bat 认为环境是好的，用户双击
得到的是一闪而过的窗口。

现在 run.bat 会真的启动一次解释器（``-c "import sys"``，与 ``test.bat`` 同一条
判据），启动不了就重建虚拟环境。本脚本用**假的、启动不了的 python.exe** 复现
那个场景，断言自愈分支确实被走到。

为了让检查快速结束（不真的下载依赖），PATH 里被去掉了 python，因此重建会失败并
走到 ``:failed``；这正是我们要看的第二条路径。

用 Python 写而不是 .bat：cmd 在处理 UTF-8 批处理 + ``findstr`` 中文匹配时有编码
坑（本脚本的第一版就因此误报），Python 里读子进程输出没有这个问题。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools._common import enable_utf8  # noqa: E402


def main() -> int:
    enable_utf8()
    if sys.platform != "win32":
        print("[skip] run.bat 是 Windows 批处理，本平台跳过")
        return 0

    root = Path(__file__).resolve().parents[1]
    run_bat = root / "run.bat"
    if not run_bat.is_file():
        print(f"[FAIL] 找不到 {run_bat}")
        return 1

    # 假的 python.exe：where.exe 的副本。`where.exe -c "import sys"` 以非零码退出，
    # 用来模拟「文件存在但解释器不可用」。
    fake_source = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "where.exe"
    if not fake_source.is_file():
        print(f"[skip] 找不到 {fake_source}，无法构造假解释器")
        return 0

    sandbox = Path(tempfile.mkdtemp(prefix="runbat-check-"))
    try:
        (sandbox / ".venv" / "Scripts").mkdir(parents=True)
        shutil.copy2(run_bat, sandbox / "run.bat")
        shutil.copy2(fake_source, sandbox / ".venv" / "Scripts" / "python.exe")

        env = dict(os.environ)
        # PATH 里不留 python：让「重建虚拟环境」这步快速失败（否则会真的去下载依赖）
        env["PATH"] = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32")
        result = subprocess.run(
            ["cmd", "/c", "run.bat", "--version"],
            cwd=str(sandbox),
            env=env,
            capture_output=True,
            timeout=120,
        )
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        print("---- run.bat 输出 ----")
        print(output.rstrip())
        print(f"---- 退出码 {result.returncode} ----")

        problems: list[str] = []
        if "无法启动" not in output:
            problems.append("run.bat 没有识别出「venv 存在但启动不了」")
        if "重建虚拟环境" not in output:
            problems.append("run.bat 没有尝试重建虚拟环境")
        if result.returncode == 0:
            problems.append("环境不可用时不该以退出码 0 结束")
        if problems:
            for item in problems:
                print(f"[FAIL] {item}")
            return 1

        print("[OK] 自愈分支按预期触发（识别损坏环境 -> 重建 -> 失败可读地报错）")
        return 0
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
