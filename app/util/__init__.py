"""子包。"""

from __future__ import annotations

import sys


def _encode_ok(stream, sample: str = "✓✗打印机") -> bool:
    """当前流能否编码我们要打印的字符（不会真的写出去）。"""
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return True  # 二进制流/被替换的对象：不插手
    try:
        sample.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def configure_stdio() -> None:
    """让控制台输出不受本地编码限制（Windows 默认 GBK / cp936）。

    自检与各命令行脚本会打印 ``✓`` ``✗`` 这类符号，以及打印机名称里的中文。
    在 GBK 控制台上直接 ``print`` 这些字符会抛 ``UnicodeEncodeError``，
    表现为「自检明明成功却以退出码 1 结束」（``selftest.bat`` 因此永远显示失败）。
    因此需要时把标准输出/错误切到 UTF-8，并让无法编码的字符降级为 ``?``。

    ## 为什么要先判断"到底需不需要"

    这里**只在当前编码确实编不出我们要打印的字符时才 reconfigure**。

    踩过的坑：无条件对 ``sys.stdout`` 调 ``reconfigure()``，会把
    **已经重定向的流**弄坏 —— 一旦输出接的是管道或文件
    （``BambuMonitor.exe --version | more``、CI 里捕获输出、``> log.txt``），
    整个命令会**一个字都输出不出来**，只在解释器退出时留下一句被忽略的
    ``Exception ignored on flushing sys.stdout: OSError: [Errno 22] Invalid argument``。
    因为这个异常发生在退出阶段、且被解释器标记为 ignored，排查时极难定位：
    表现是"命令成功了但没有任何输出"。

    UTF-8 控制台上再 reconfigure 也是多余的（本来就是 UTF-8）。

    可在任意入口重复调用；幂等。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover - 极老的解释器/被替换的流对象
            continue
        if _encode_ok(stream):
            continue  # 当前编码够用（UTF-8 控制台、或输出已重定向），别去动它
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 流已关闭或不可配置
            pass
        # 立刻 flush 一次：把"流不可用"的问题暴露在这里（能捕获），
        # 而不是拖到解释器退出时变成一句被忽略的警告
        try:
            stream.flush()
        except (ValueError, OSError):
            pass
