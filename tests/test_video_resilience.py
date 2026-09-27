"""画面「卡住 / 不显示」的几个已确认原因（都是真机上复现出来的）。

## 这份文件针对的四件事

1. **`max_fps = 0` 被当成「不取帧」** —— 设置界面里 0 的说明是"不限制帧率"，
   而轮询型设备族（Klipper / Moonraker）的循环写的是 `if self._max_fps > 0`。
   真机复现：Voron 的遥测、详细读数全正常，画面永远停在「画面未启动」；
   把帧率改成非 0 就立刻好了。
2. **流在重连 ≠ 画面断了** —— RTSPS 的打印机会不定期拒掉新连接（真机三台 X2D 都这样），
   于是状态在 connecting / streaming 之间来回跳。老代码只要状态不是 streaming 就把
   `camera_online` 置假，界面就一直闪「画面重连中」，而画面其实还在动。
3. **桌面版自己起的网页服务一个回调都没传** —— 用户的原话是「桌面版上网页还是有一些
   功能不可用，例如重新连接打印机画面」，根因就是这个：添加/删除/编辑/重连、诊断、
   刷新画面、全部连接断开、画面顺序、配置导入导出在桌面版的网页上全部返回
   「该运行方式不支持」。
4. **网页端改了设备列表，桌面界面不同步** —— 两边共用一份配置，但桌面不重排，
   于是"网页加的设备桌面看不到、网页删的桌面还留着"。
5. **慢通道（6000 端口的 P1/A1）被判成离线** —— 判据写死 6 秒，而这类机器
   只有 0.2~0.5 fps（两帧之间本来就等 2~5 秒），于是画面在动、状态却显示离线。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from app.bambu.models import PrinterInfo, PrinterModel, PrinterStatus  # noqa: E402
from app.bambu.printer import PrinterSession, frame_stale_limit  # noqa: E402
from app.bambu.timeouts import (  # noqa: E402
    FRAME_STALE_TOLERANCE,
    FRAME_STALE_TOLERANCE_MAX,
)

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="界面测试需要 PySide6（CI 不装它）")


def _config():
    """一份**不写盘**的配置（测试绝不能碰用户真实的 config.json）。"""
    from app.config import AppConfig

    config = AppConfig()
    config.persist = False
    return config


class FakeStream:
    """只提供 ``last_frame_age`` / ``fps`` / ``frame_gap`` 的流替身。"""

    def __init__(self, age: float, fps: float = 0.0, gap: float = 0.0) -> None:
        self.last_frame_age = age
        self.fps = fps
        self.frame_gap = gap


def _session() -> PrinterSession:
    return PrinterSession(
        PrinterInfo(ip="192.168.31.110", name="X2D", model=PrinterModel.X2D, access_code="12345678")
    )


def _x1c() -> PrinterSession:
    """X1C 是 `auto` 机型（老固件靠 6000、新固件只有 RTSPS）。"""
    return PrinterSession(
        PrinterInfo(ip="192.168.31.27", name="X1C", model=PrinterModel.X1C, access_code="12345678")
    )


# --------------------------------------------------- ① 重连中不算"画面断了"
def test_重连中但画面还新时保持在线():
    """契约：状态是 connecting/retrying，但上一帧还很新 → 仍然算在线（别让界面闪）。"""
    session = _session()
    session._rtsp = FakeStream(age=0.5)
    session._handle_stream_state(session._rtsp, "retrying", "RTSPS 画面中断，正在重连")
    assert session.status.camera_online is True, "画面还在动，不该显示成断线"
    assert session.last_camera_detail  # 但原因要留着让人看到


def test_重连且画面确实旧了才置为离线():
    session = _session()
    session._rtsp = FakeStream(age=FRAME_STALE_TOLERANCE + 5)
    session._handle_stream_state(session._rtsp, "retrying", "RTSPS 画面中断，正在重连")
    assert session.status.camera_online is False


def test_真正开始推流就是在线():
    session = _session()
    session._rtsp = FakeStream(age=999)
    session._handle_stream_state(session._rtsp, "streaming", "RTSPS 已连接")
    assert session.status.camera_online is True


def test_停止与鉴权错误一律离线():
    session = _session()
    session._rtsp = FakeStream(age=0.1)  # 即使帧很新
    session._handle_stream_state(session._rtsp, "stopped", "已停止")
    assert session.status.camera_online is False
    session._handle_stream_state(session._rtsp, "auth_error", "口令不对")
    assert session.status.camera_online is False


def test_别的通道的状态不会覆盖当前通道():
    """契约：旧通道收尾时回调过来，不能把新通道的状态改掉。"""
    session = _session()
    session._rtsp = FakeStream(age=0.1)
    session.last_camera_state = "streaming"  # 当前通道正在推流
    session.last_camera_detail = "画面正常"
    session._handle_stream_state(object(), "stopped", "旧通道已停止")
    assert session.last_camera_state == "streaming"
    assert session.last_camera_detail == "画面正常"


# --------------------------------------------------- ③ 慢通道不能按 6 秒判离线
#
# 真机复现（用户家里 17 台设备，从正在运行的实例里量出来的）：6000 端口的
# A1 / P1 系列间隔中位数 2.8~8.8 秒、**最大 12~21 秒**，十几秒没有新帧是常态；
# 而判据写死 6 秒（`snapshot()` 里那句 `last_frame_age < 6.0`），于是每台
# 6000 端口的设备都在"在线/离线"之间乱闪（90 秒里翻 2~8 次），画面其实一直在动。
def test_门槛按设备自己的节奏放宽():
    """``frame_stale_limit``：快流沿用 6 秒，慢流按实测最大间隔放宽并封顶。"""
    assert frame_stale_limit(0) == FRAME_STALE_TOLERANCE  # 还没出过帧
    assert frame_stale_limit(9.0) == FRAME_STALE_TOLERANCE  # RTSPS：≈9 fps
    # 6000 端口：中位 3 秒、偶尔 13 秒一张（实测 p1s 6）
    assert frame_stale_limit(0.5, gap=13.0) == pytest.approx(19.5)
    # A1：最大间隔 21 秒 → 1.5 倍会顶到上限
    assert frame_stale_limit(0.2, gap=21.0) == FRAME_STALE_TOLERANCE_MAX
    # 只给帧率（还没有间隔数据）时用 1/fps 兜底：0.2 fps → 5 秒 × 1.5 = 7.5
    assert frame_stale_limit(0.2) == pytest.approx(7.5)
    # 帧率很高、又没有间隔数据 → 退回 6 秒下限
    assert frame_stale_limit(9.0) == FRAME_STALE_TOLERANCE
    # 脏数据不能算出怪值
    assert frame_stale_limit("x", gap=None) == FRAME_STALE_TOLERANCE  # type: ignore[arg-type]
    assert frame_stale_limit(-3.0, gap=-1.0) == FRAME_STALE_TOLERANCE


def test_慢流两帧之间保持在线():
    """**回归**：6000 端口的机器上一帧 13 秒前，仍然是在线（旧判据会显示离线）。"""
    session = _session()
    session._camera = FakeStream(age=13.0, fps=0.2, gap=13.0)
    assert session._camera.last_frame_age > FRAME_STALE_TOLERANCE  # 确实超过旧的 6 秒判据
    assert session.snapshot().camera_online is True


def test_慢流真的断了还是判离线():
    """放宽不等于不管：超过上限（30 秒）没有新帧就必须显示离线。"""
    session = _session()
    session._camera = FakeStream(age=FRAME_STALE_TOLERANCE_MAX + 5.0, fps=0.2, gap=6.0)
    assert session.snapshot().camera_online is False


def test_快流仍然按6秒判离线():
    """契约：RTSPS 这类快流不能因为这次放宽而变得迟钝。"""
    session = _session()
    session._rtsp = FakeStream(age=FRAME_STALE_TOLERANCE + 0.5, fps=9.0, gap=0.3)
    assert session.snapshot().camera_online is False
    session._rtsp = FakeStream(age=1.0, fps=9.0, gap=0.3)
    assert session.snapshot().camera_online is True


def test_没有_OpenCV_时的_H264_通道也算在线():
    """**回归**：``snapshot()`` 以前只看 `_camera`/`_rtsp`，安卓版走 H.264 通路
    （`_h264`）时画面在动却没人更新在线状态。"""
    session = _session()
    session._h264 = FakeStream(age=0.4, fps=8.0, gap=0.3)
    assert session.snapshot().camera_online is True
    session._h264 = FakeStream(age=30.0, fps=8.0, gap=0.3)
    assert session.snapshot().camera_online is False


def test_切换通路的瞬间按最新那帧判():
    """契约：通道切换时一新一旧两条流可能同时挂着，不能被旧流带偏。"""
    session = _session()
    session._camera = FakeStream(age=40.0, fps=0.2, gap=6.0)  # 已经停掉的旧通道
    session._h264 = FakeStream(age=0.3, fps=8.0, gap=0.3)  # 刚接上的新通道
    assert session.snapshot().camera_online is True
    session._handle_stream_state(session._h264, "retrying", "正在重连")
    assert session.status.camera_online is True, "新通道画面还新，不该被旧通道的陈旧帧判离线"


# --------------------------------------------------- ④ auto 机型别再往 6000 白退
#
# 真机复现（X1C，192.168.31.27）：X1 系在机型表里是 `auto`（老固件靠 6000 取画面），
# 于是 RTSPS 一失败就退到 6000；而新固件的 6000 端口**用正确口令也会被拒**，
# 只好等看门狗（最多 15 秒一轮）再切回 RTSPS。表现就是状态永远在
# 「连接中」与「访问代码错误」之间跳，画面一次也出不来。
def test_6000_被拒过就不再选它():
    """契约：坐实 6000 走不通之后，`auto` 机型的首选通道直接是 RTSPS。"""
    session = _x1c()
    assert session._preferred_channel() in ("rtsp", "tcp6000")  # 还没有证据时按老规矩
    session.status.mqtt_online = True
    session._camera = FakeStream(age=1e9)
    session._handle_stream_state(
        session._camera, "auth_error", "打印机拒绝连接：请检查访问代码（局域网访问码）是否正确"
    )
    assert session._tcp6000_rejected is True
    assert session._preferred_channel() == "rtsp", "已经证实 6000 走不通，不该再退回去"
    assert session._effective_channel() == "rtsp"


def test_口令真的错了不会被当成机型不支持():
    """契约：遥测没连上（口令本身错）时不能记成「6000 不支持该机型」。"""
    session = _x1c()
    session.status.mqtt_online = False
    session._camera = FakeStream(age=1e9)
    session._handle_stream_state(session._camera, "auth_error", "打印机拒绝连接：口令不对")
    assert session._tcp6000_rejected is False, "口令错是用户要改配置，不是机型问题"


def test_只有6000一条路的机型不受影响():
    """契约：P1S / A1 这类机型无论发生什么都不会被改到 RTSPS 上（它们在 6000 上才好）。"""
    session = PrinterSession(
        PrinterInfo(ip="192.168.31.50", name="p1s", model=PrinterModel.P1S, access_code="12345678")
    )
    session._tcp6000_rejected = True
    assert session._effective_channel() == "tcp6000"
    assert session._preferred_channel() == "tcp6000"


# --------------------------------------------------- ② 桌面版的网页服务要接全回调
#
# 这两个用例用**源码契约**而不是真的构造 MainWindow：构造真窗口会起一堆真实会话
# 线程，测试收尾时 Qt 会 fail-fast（整个 pytest 进程没、无 traceback），
# 本仓库对这类代码的既有做法就是静态断言（见 tests/test_ui_thread_shutdown.py 的说明）。
def _start_web_server_block() -> str:
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    source = (root / "app" / "ui" / "main_window.py").read_text(encoding="utf-8")
    after = source.split("def start_web_server", 1)[1]
    return after.split("\n    def ", 1)[0]


def test_桌面版的网页服务接上了全部回调():
    """**回归**：桌面版以前一个回调都不传，于是网页上十来个功能全是「不支持」。

    用户的原话：「桌面版上网页还是有一些功能不可用，例如重新连接打印机画面」。
    """
    block = _start_web_server_block()
    for key in (
        "discover_fn",
        "add_printer_fn",
        "manage_printer_fn",
        "get_settings_fn",
        "update_settings_fn",
        "export_config_fn",
        "import_config_fn",
        "diagnose_fn",
        "layout_fn",
        "camera_action_fn",
        "reorder_fn",
        "sessions_action_fn",
        "info_fn",
    ):
        assert f"{key}=" in block, f"桌面版没有把 {key} 接到网页服务上"


def test_桌面版用同一套WebHost并挂了同步钩子():
    """契约：桌面版复用无界面版那套 WebHost，并在配置被改后同步界面。"""
    block = _start_web_server_block()
    assert "WebHost(" in block, "桌面版应当复用 WebHost，而不是自己再写一套设备管理"
    assert "on_change=" in block, "网页改了配置之后必须回调桌面界面去同步"
    assert "_on_web_host_change" in block


def test_网页端改设置在桌面侧生效():
    """契约：网页改了帧率/刷新间隔，桌面运行时状态要跟着变。"""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    source = (root / "app" / "ui" / "main_window.py").read_text(encoding="utf-8")
    block = source.split("def _update_settings_from_web", 1)[1].split("\n    def ", 1)[0]
    assert "set_max_fps" in block, "改了每路帧率却没作用到会话上"
    assert "_refresh_timer" in block, "改了界面刷新间隔却没作用到定时器上"
    assert "set_fps" in block, "改了网页帧率却没作用到转码线程上"


def test_配置被外部改动后桌面会重建监控墙():
    """契约：网页端增删设备/改顺序之后，桌面要按配置重建（否则两边显示不一致）。"""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    source = (root / "app" / "ui" / "main_window.py").read_text(encoding="utf-8")
    assert "def reload_from_config" in source
    block = source.split("def reload_from_config", 1)[1].split("\n    def ", 1)[0]
    assert "_detach_tile" in block, "配置里删掉的设备要从界面上摘掉"
    assert "add_printer" in block, "配置里新增的设备要补上卡片"
    assert "rebuild_grid" in block, "顺序变了要重排"
    # 会话对象必须沿用（重建会话=每次同步都重连一遍，画面白闪）
    assert "create_session" not in block
