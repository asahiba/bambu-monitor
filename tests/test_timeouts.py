"""``app/bambu/timeouts.py`` 的契约测试。

## 为什么单独立一条

超时常量表最容易「建了但没人用」：改了 `timeouts.py` 里的数字，协议层还写着自己
那份字面量，看起来收敛了、实际上没生效。所以这里除了检查取值合理，还要**抽查
关键调用点真的引用常量**（用默认参数与 ``inspect`` 反查，而不是读源码文本）。

## 不测什么

不测具体数值「对不对」——那取决于打印机固件与网络，属于调优；
这里只锁住**关系**（重连等待必须比停止等待宽、切片必须比总超时短）与**类型**。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.bambu import camera, discovery, mqtt_worker, probe, rtsp, timeouts

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "app" / "bambu" / "timeouts.py"

pytestmark = pytest.mark.usefixtures("no_network")


def _constants() -> dict[str, object]:
    return {
        name: value
        for name, value in vars(timeouts).items()
        if name.isupper() and not name.startswith("_")
    }


def test_模块里只有数值常量():
    """契约：这张表只放数字，不放函数/字符串（避免变成第二个「杂物间」）。"""
    for name, value in _constants().items():
        assert isinstance(value, (int, float)), f"{name} 不是数值：{value!r}"
        assert not isinstance(value, bool), f"{name} 是布尔值，明显写错了"
        assert value > 0, f"{name} 必须为正数：{value!r}"


def test_常量数量够用但不失控():
    """提示性断言：常量太少说明收敛没做完，太多说明在堆砌。"""
    assert 15 <= len(_constants()) <= 40


def test_模块不导入任何其它app模块():
    """契约（与 ports.py 同一条纪律）：协议层、界面、命令行都要能安全引用它。"""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    offenders = [name for name in imported if name.startswith("app") or name.startswith(".")]
    assert not offenders, f"timeouts.py 不应导入其它 app 模块：{offenders}"


def test_等待线程收尾的时间不会倒挂():
    """契约：重连要等得比停止更久，否则上一轮线程还在跑就开了新一轮。"""
    assert timeouts.VIDEO_RESTART_JOIN > timeouts.VIDEO_STOP_JOIN


def test_读切片必须明显小于整帧超时():
    """契约：``CAMERA_READ_SLICE`` 是「多久回头看一眼停止标志」。

    它必须远小于帧超时，否则「停止」要等很久才生效（关闭窗口卡住）。
    """
    assert timeouts.CAMERA_READ_SLICE <= timeouts.CAMERA_FRAME_HEADER_TIMEOUT / 2


def test_画面首帧等待不超过单帧等待():
    """探测/诊断等首帧不能比正常读一帧还久，否则用户白等。"""
    assert timeouts.CAMERA_FIRST_FRAME_TIMEOUT <= timeouts.CAMERA_FRAME_BODY_TIMEOUT
    assert timeouts.RTSP_FIRST_FRAME_TIMEOUT <= timeouts.RTSP_ADOPT_TIMEOUT


def test_看门狗间隔明显大于_paho_的退避上限():
    """契约：判据是「这么久没连上过」，太短会跟 paho 自己的重连打架。"""
    assert timeouts.MQTT_STUCK_SECONDS >= 60.0
    assert timeouts.WATCHDOG_INTERVAL < timeouts.MQTT_STUCK_SECONDS


# --------------------------------------------------------------------------- 常量真的被用上


def test_画面路径的默认参数来自常量表():
    signature = inspect.signature(camera.grab_single_frame)
    assert signature.parameters["timeout"].default == timeouts.CAMERA_FIRST_FRAME_TIMEOUT

    read_exact = inspect.signature(camera.CameraStream._read_exact)
    assert read_exact.parameters["timeout"].default == timeouts.CAMERA_FRAME_BODY_TIMEOUT


def test_rtsp_路径的默认参数来自常量表():
    signature = inspect.signature(rtsp.RtspStream.__init__)
    assert signature.parameters["open_timeout_ms"].default == timeouts.RTSP_OPEN_TIMEOUT_MS
    assert (
        inspect.signature(rtsp.RtspStream.wait_first_frame).parameters["timeout"].default
        == timeouts.RTSP_FIRST_FRAME_TIMEOUT
    )


def test_模块级常量被_import_到使用方():
    """抽查：会话与遥测模块引用的是同一份定义（同一对象，而不是又写了一遍）。"""
    from app.bambu import printer

    assert printer.MQTT_STUCK_SECONDS == timeouts.MQTT_STUCK_SECONDS
    assert printer.MQTT_STUCK_SECONDS is timeouts.MQTT_STUCK_SECONDS


def test_探测与诊断不再各写一份字面量():
    """契约：探测默认超时与诊断各步超时都必须来自常量表。

    诊断流程现在只有一份实现（`app/bambu/diagnostics.py`），CLI 与界面共用，
    所以这里盯的是它 + 添加对话框。

    ⚠️ `app.ui.*` 需要 PySide6：CI 刻意不装它，所以界面那部分断言要**跳过**
    而不是报错（否则整条用例会因为 ModuleNotFoundError 变成失败）。
    """
    from app.bambu import diagnostics

    assert diagnostics.DIAG_TCP_TIMEOUT is timeouts.DIAG_TCP_TIMEOUT
    assert diagnostics.PROBE_MQTT_TIMEOUT is timeouts.PROBE_MQTT_TIMEOUT

    try:
        from app.ui import add_dialog, diagnose_dialog
    except ImportError as exc:  # 没有 Qt 的环境（CI）
        print(f"（跳过界面部分：{exc}）")
        return

    assert add_dialog.PROBE_MQTT_TIMEOUT is timeouts.PROBE_MQTT_TIMEOUT
    # 诊断对话框把超时全部委托给共用模块，自己不再直接引用超时常量
    assert not hasattr(diagnose_dialog, "DIAG_TCP_TIMEOUT"), (
        "对话框不该再自己定义/引用诊断超时 —— 那是 app/bambu/diagnostics.py 的事"
    )


def test_各模块不再硬编码同一类超时(monkeypatch):
    """回归：协议层文件里不应残留被收敛掉的字面量。

    只在**参数默认值**与已收敛的调用点上检查（用 AST 找 ``timeout=数字``），
    避免误伤业务上确实需要独立数字的地方（例如看门狗间隔就是这么一类）。
    """
    watched = {
        "app/bambu/camera.py": [],
        "app/bambu/rtsp.py": [],
        "app/bambu/mqtt_worker.py": ["5.0", "4.0"],
        "app/bambu/discovery.py": [],
        "app/bambu/diagnostics.py": ["4.0", "10.0", "3.0", "5.0", "6.0"],
        "app/ui/diagnose_dialog.py": [],
        "app/ui/add_dialog.py": ["10.0"],
    }
    leftover: list[str] = []
    for relative, forbidden in watched.items():
        if not forbidden:
            continue
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg != "timeout":
                        continue
                    if isinstance(keyword.value, ast.Constant) and repr(keyword.value.value) in forbidden:
                        leftover.append(f"{relative}: timeout={keyword.value.value!r}")
    assert not leftover, "这些超时应当来自 app/bambu/timeouts.py：" + "；".join(leftover)


def test_discovery_的间隔也来自常量表():
    """``discovery.py`` 里那几个 sleep 以前是散落的 0.05 / 0.1 / 1.5。"""
    source = (PROJECT_ROOT / "app" / "bambu" / "discovery.py").read_text(encoding="utf-8")
    for literal in ("time.sleep(0.05)", "time.sleep(0.1)", "timeout=1.5"):
        assert literal not in source, f"{literal} 应当改用 timeouts.py 里的常量"


def test_各使用方导入的名字都真实存在():
    """防拼写错误：使用方的 ``from .timeouts import ...`` 里每个名字都要存在。

    （导入本身就会失败，所以这条更像是「文档」：它把「谁引用了哪几个常量」
    列出来，改名字时能立刻看到影响面。）
    """
    available = set(vars(timeouts))
    users = [
        "app/bambu/camera.py",
        "app/bambu/printer.py",
        "app/bambu/probe.py",
        "app/bambu/rtsp.py",
        "app/bambu/mqtt_worker.py",
        "app/bambu/discovery.py",
        "app/bambu/diagnostics.py",
        "app/ui/add_dialog.py",
        "app/ui/diagnose_dialog.py",
    ]
    used: set[str] = set()
    for relative in users:
        tree = ast.parse((PROJECT_ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("timeouts"):
                names = [alias.name for alias in node.names]
                missing = [name for name in names if name not in available]
                assert not missing, f"{relative} 导入了不存在的常量：{missing}"
                used.update(names)
    # 反向检查：这张表不是装饰品 —— 每个常量都真的有人用
    unused = sorted(name for name in _constants() if name not in used)
    assert not unused, f"这些常量没有任何使用方（收敛没做完？）：{unused}"
