"""校验 GitHub 工作流里 pwsh 步骤的 PowerShell 语法（开发辅助脚本）。

为什么需要：工作流里的 `run: |` 块是纯文本，本地没有编译期检查 ——
写错一个括号只会等到 CI 跑起来才炸。这个脚本把每个 pwsh 步骤抽出来，
交给 Windows PowerShell 5.1 的解析器过一遍。

用法：python tools/check_workflow_ps.py

需要 `PyYAML`（在 `requirements-dev.txt` 里）与 `powershell.exe`（Windows）。
两者缺一时给出**可读的退出码 0/跳过后提示**，而不是抛 traceback ——
它以前是直接 `import yaml`，在没装的机器上只会甩一段堆栈，看不出该装什么。
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools._common import enable_utf8  # noqa: E402

enable_utf8()

try:
    import yaml
except ImportError:  # pragma: no cover - 只在没装 PyYAML 时走到
    print("[skip] 缺少 PyYAML，无法解析工作流。安装：")
    print("       .venv\\Scripts\\python.exe -m pip install PyYAML")
    sys.exit(0)

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

#: 用 PowerShell 5.1 解析（工作流里的 shell: pwsh 是 7，但 7 更宽容；
#: 用 5.1 校验会更严格，能提前发现只在 5.1 上出问题的写法）。
CHECKER = r"""
param([string]$Path)
$e = $null
[void][System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$null, [ref]$e)
if ($e.Count) {
    $e | Select-Object -First 3 | ForEach-Object { "FAIL {0}: {1}" -f $_.Extent.StartLineNumber, $_.Message }
} else {
    "OK"
}
"""


def main() -> int:
    if shutil.which("powershell.exe") is None:
        print("[skip] 本平台没有 powershell.exe（工作流语法校验需要 Windows PowerShell）")
        return 0
    if not WORKFLOWS:
        print("[skip] 没找到 .github/workflows/*.yml")
        return 0

    checker_path = pathlib.Path(tempfile.gettempdir()) / "dsh_ps_check.ps1"
    checker_path.write_text(CHECKER, encoding="utf-8")
    body_path = pathlib.Path(tempfile.gettempdir()) / "dsh_ps_body.ps1"

    failures = 0
    checked = 0
    for workflow in WORKFLOWS:
        doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        for job_name, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if step.get("shell") != "pwsh" or "run" not in step:
                    continue
                checked += 1
                label = f"{workflow.name}::{job_name}::{step.get('name') or '?'}"
                # ⚠️ 两个坑都踩过，缺一不可：
                #   1. 用 **UTF-8 BOM** 写临时文件 —— 步骤里有中文（throw 的消息），
                #      而 PowerShell 5.1 读无 BOM 的 UTF-8 会按 GBK 解码，
                #      中文字符被拆坏后连带破坏引号配对，报出
                #      "The string is missing the terminator"（就是本文件
                #      第一次跑出来的假警报，和 tests/test_contracts.py 里
                #      那条 BOM 契约测试拦的是同一类问题）；
                #   2. 用 CRLF 换行 —— PS 5.1 要求 here-string（@" … "@）的
                #      结束符前是 CRLF，只给 LF 同样会误报同一个错。
                body = step["run"].replace("\r\n", "\n").replace("\n", "\r\n")
                body_path.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
                result = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", str(checker_path), str(body_path)],
                    capture_output=True, text=True,
                )
                output = (result.stdout or result.stderr).strip()
                if output == "OK":
                    print(f"  [OK]   {label}")
                else:
                    failures += 1
                    print(f"  [FAIL] {label}")
                    for line in output.splitlines()[:4]:
                        print(f"           {line}")

    print()
    print(f"检查了 {checked} 个 pwsh 步骤，语法有问题 {failures} 个")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
