"""子包。"""

from __future__ import annotations

import sys


def configure_stdio() -> None:
    """让控制台输出不受本地编码限制（Windows 默认 GBK / cp936）。

    自检与各命令行脚本会打印 ``✓`` ``✗`` 这类符号，以及打印机名称里的中文。
    在 GBK 控制台上直接 ``print`` 这些字符会抛 ``UnicodeEncodeError``，
    表现为「自检明明成功却以退出码 1 结束」（``selftest.bat`` 因此永远显示失败）。
    这里统一把标准输出/错误切到 UTF-8，并让无法编码的字符降级为 ``?``
    而不是让整个命令崩掉。

    可在任意入口重复调用；对已关闭/被重定向的流保持静默。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover - 极老的解释器/被替换的流对象
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 流已关闭或不可配置
            pass
