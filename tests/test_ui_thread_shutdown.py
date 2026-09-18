"""契约：对话框里的后台线程不能在运行中被销毁（否则整个进程 fail-fast 崩掉）。

真实症状（用户可复现）：

    点开「通道诊断」→ 随手关掉 → 程序整个消失，没有报错、没有日志。

原因：``closeEvent`` 里 ``thread.wait(3000)`` 只等 3 秒，而一轮诊断要 20 秒以上
（3 个端口 × TLS 4 秒 + 摄像头 12 秒 + MQTT 10 秒）。等不到就继续走析构流程，
**运行中的 QThread 被析构会让 Qt 直接 fail-fast**（Windows 上是 0xC0000409）。
「添加打印机 → 测试连接 → 立刻关闭」也是同一条路径。

这类崩溃会直接杀掉 pytest 进程，所以必须用**子进程**跑：
断言子进程退出码为 0，而不是断言某个返回值。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SNIPPETS = {
    "diagnose": """
import os, sys, time
sys.path.insert(0, {root!r})
from PySide6.QtWidgets import QApplication
from app.bambu.models import PrinterInfo
from app.ui.diagnose_dialog import DiagnoseDialog

app = QApplication([])
info = PrinterInfo(ip="127.0.0.1", name="离线机", access_code="12345678")
dialog = DiagnoseDialog(info)
dialog.close()          # 线程还在跑就关掉：这里原来会崩
app.processEvents()
time.sleep(1.0)
print("SURVIVED", flush=True)
""",
    "add_dialog": """
import os, sys, time
sys.path.insert(0, {root!r})
from PySide6.QtWidgets import QApplication
from app.ui.add_dialog import PrinterEditDialog

app = QApplication([])
dialog = PrinterEditDialog()
dialog.ip_edit.setText("127.0.0.1")
dialog.code_edit.setText("12345678")
dialog._start_test()    # 启动探测线程（目标不可达，会走超时路径）
dialog.close()          # 线程还在跑就关掉
app.processEvents()
time.sleep(1.0)
print("SURVIVED", flush=True)
""",
}

#: Windows 下 Qt fail-fast 的退出码（STATUS_STACK_BUFFER_OVERRUN）
FAIL_FAST = 0xC0000409


def _run(snippet: str, tmp_path: Path) -> subprocess.CompletedProcess:
    script = tmp_path / "probe.py"
    script.write_text(snippet.format(root=str(PROJECT_ROOT)), encoding="utf-8")
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["BAMBU_MONITOR_CONFIG_DIR"] = str(tmp_path / "config")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )


def test_diagnose_dialog_close_while_running_does_not_crash(tmp_path):
    """点开诊断再随手关掉，进程必须活着。"""
    result = _run(SNIPPETS["diagnose"], tmp_path)
    assert result.returncode == 0, (
        f"诊断对话框关掉了正在运行的线程，进程异常退出（{result.returncode:#x}）：\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "SURVIVED" in result.stdout


def test_add_dialog_close_while_probing_does_not_crash(tmp_path):
    """点「测试连接」后立刻关掉对话框，进程必须活着。"""
    result = _run(SNIPPETS["add_dialog"], tmp_path)
    assert result.returncode == 0, (
        f"添加对话框关掉了正在运行的探测线程，进程异常退出（{result.returncode:#x}）：\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "SURVIVED" in result.stdout
