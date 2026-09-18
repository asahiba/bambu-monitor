"""让「对话框关掉后仍在收尾的 QThread」不会把整个进程带走。

## 背景

对话框里的后台线程（诊断、测试连接）要跑十几秒，用户往往中途就把窗口关了。
Qt 的硬性规则：**运行中的 QThread 被析构 = 直接 fail-fast**，进程没有任何
Python traceback 就消失（Windows 退出码 ``0xC0000409``）。用户看到的是
「点开诊断、随手关掉 → 程序没了」，完全无从排查。

只把线程 ``setParent(QApplication.instance())`` 还不够：线程仍可能比进程活得久，
解释器退出时它照样被销毁。所以这里再补一层兜底：

* ``retire_thread()`` 把线程登记进「退休名单」，并解除父子关系；
* 进程退出前（``atexit``）按名单等它自然结束（有上限，不会真的挂住退出）。

正常使用（关掉对话框、程序继续跑）时这层完全不参与 —— 线程跑完自行消失。
"""

from __future__ import annotations

import atexit
import logging
import threading
from typing import Optional

LOGGER = logging.getLogger("bambu-monitor.qthreads")

#: 退出前最多等多久（秒）。超时就放弃等待 —— 宁可留下一个线程，
#: 也不要让用户觉得程序卡在退出上。
SHUTDOWN_WAIT = 6.0

_retired: list = []
_lock = threading.Lock()
_hook_installed = False


def _on_exit() -> None:
    """进程退出前，等退休线程跑完（避免 Qt 析构运行中的线程）。"""
    with _lock:
        pending = list(_retired)
    for thread in pending:
        try:
            if thread.isRunning():
                LOGGER.debug("退出前等待后台线程结束：%s", thread.__class__.__name__)
                thread.wait(int(SHUTDOWN_WAIT * 1000))
        except RuntimeError:
            # 底层 C++ 对象已经没了，忽略
            pass


def retire_thread(thread: Optional[object]) -> None:
    """把一个仍在运行的 QThread 交给退休名单（可安全重复调用）。"""
    global _hook_installed
    if thread is None:
        return
    try:
        if not thread.isRunning():
            return
        # 解除与对话框的父子关系：否则对话框析构时会连带析构它
        thread.setParent(None)
        with _lock:
            if thread not in _retired:
                _retired.append(thread)
                thread.finished.connect(lambda: _forget(thread))
        if not _hook_installed:
            atexit.register(_on_exit)
            _hook_installed = True
        LOGGER.debug("后台线程转入退休名单：%s", thread.__class__.__name__)
    except RuntimeError:
        pass


def _forget(thread: object) -> None:
    with _lock:
        if thread in _retired:
            _retired.remove(thread)


def retired_count() -> int:
    """当前还在退休名单里的线程数（测试用）。"""
    with _lock:
        return len(_retired)
