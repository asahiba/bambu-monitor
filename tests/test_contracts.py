"""接口契约快照：为多设备族重构立的护栏。

## 为什么需要

接下来要把「只支持拓竹」重构为「支持多个设备族」，会大范围搬动
`models.py` / `printer.py` / `discovery.py` 的实现。这些模块的**对外接口**
（UI、网页、命令行、配置文件都在用）必须保持不变，否则用户可见行为会悄悄坏掉。

本文件**不测业务逻辑**（那是其它测试文件的事），只锁定"接口形状"：

* `PrinterSession` 对外成员名集合 —— UI 与网页的调用面
* 网页 `/api/printers` 载荷的键集合 —— 前端 JS 的契约
* 网页页面里的 DOM 标识与按钮动作名 —— 内嵌 JS 没有编译期检查，最容易静默坏
* `PrinterModel` 成员与三个能力属性 —— 机型表契约
* 网页控制指令白名单 —— 前端能点的按钮必须都有后端分支

**如果这些测试失败**：先确认你是否有意要改对外接口。若是有意的，请同步更新
本文件、`docs/ARCHITECTURE.md` 的「牵连清单」、以及前端 `app/web/page.py`；
若是无意的，说明重构碰坏了对外契约，应当修回来而不是改测试。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- 会话接口


#: UI 与网页实际调用的成员（`docs/ARCHITECTURE.md` 第 2 节）。
#: 加成员是安全的；删或改名会让界面/网页静默失效，因此在这里锁死。
SESSION_PUBLIC_SURFACE = frozenset(
    {
        # 生命周期
        "start",
        "stop",
        "restart",
        # 查询
        "snapshot",
        "latest_frame",
        "connection_summary",
        "camera_fps",
        "video_backend",
        "can_control",
        # 控制
        "pause_print",
        "resume_print",
        "stop_print",
        "set_light",
        "set_speed",
        # 视频参数
        "set_max_fps",
        "set_video_target_size",
    }
)

#: 会话实例上必须存在的属性（UI/网页直接读）
SESSION_ATTRIBUTES = frozenset(
    {
        "info",
        "status",
        "warnings",
        "running",
        "mqtt_auth_error",
        "last_camera_state",
        "last_camera_detail",
    }
)


def test_会话公开接口未缩减():
    """契约：`PrinterSession` 必须继续提供 UI/网页依赖的全部公开成员。

    重构时把实现搬进适配器是可以的，但**不能**把这些名字从会话上拿掉。
    """
    from app.bambu.printer import PrinterSession

    missing = sorted(name for name in SESSION_PUBLIC_SURFACE if not hasattr(PrinterSession, name))
    assert not missing, f"会话公开接口缺失：{missing}（UI/网页依赖它们）"


def test_会话实例属性存在():
    """契约：会话实例必须暴露 UI/网页直接读取的属性。"""
    from app.bambu.models import PrinterInfo
    from app.bambu.printer import PrinterSession

    session = PrinterSession(PrinterInfo(ip="127.0.0.1", serial="01P00A123456789"))
    missing = sorted(name for name in SESSION_ATTRIBUTES if not hasattr(session, name))
    assert not missing, f"会话实例属性缺失：{missing}"


def test_偏好通道方法仍可用():
    """契约：`_preferred_channel()` 是通道选择的入口，测试与诊断都依赖它。

    重构后它应当转发给适配器，而不是消失。
    """
    from app.bambu.models import PrinterInfo, PrinterModel
    from app.bambu.printer import PrinterSession

    session = PrinterSession(PrinterInfo(ip="127.0.0.1", model=PrinterModel.P1S))
    assert session._preferred_channel() == "tcp6000"


# --------------------------------------------------------------------------- 机型表


#: 已支持的机型标签。新增机型时在这里补一行（顺带确认 6 处同步点，见 models.py 文档）。
KNOWN_MODEL_LABELS = frozenset(
    {
        "X1C",
        "X1",
        "X1E",
        "P1P",
        "P1S",
        "A1",
        "A1 mini",
        "A2L",
        "P2S",
        "H2D",
        "H2D Pro",
        "H2C",
        "H2S",
        "X2D",
    }
)


def test_机型表未丢失成员():
    """契约：已支持的机型一个都不能从枚举里消失（老配置里存着这些标签）。"""
    from app.bambu.models import PrinterModel

    labels = {model.label for model in PrinterModel}
    assert labels >= KNOWN_MODEL_LABELS, f"机型枚举缺少：{sorted(KNOWN_MODEL_LABELS - labels)}"
    assert PrinterModel.UNKNOWN.label == "未知机型", "未知机型的标签是配置兼容性的一部分"


@pytest.mark.parametrize("model_label", sorted(KNOWN_MODEL_LABELS))
def test_每个机型都有完整能力声明(model_label):
    """契约：每个机型都必须能回答「通道 / RTSPS / 腔温 / 是否已知」四个问题。

    新增机型最容易漏的就是这几个属性，漏了会在运行时才炸。
    """
    from app.bambu.models import PrinterModel

    model = PrinterModel(model_label)
    assert model.video_channel in ("tcp6000", "rtsp", "auto")
    assert isinstance(model.supports_rtsp, bool)
    assert isinstance(model.has_chamber_sensor, bool)
    assert model.is_known is True


# --------------------------------------------------------------------------- 网页载荷


def _fake_session():
    """构造一个内容填满的假会话，用来生成完整的网页载荷。"""
    from app.bambu.models import AmsTray, HmsItem, PrinterInfo, PrinterModel, PrinterStatus

    class _FakeSession:
        def __init__(self) -> None:
            self.info = PrinterInfo(
                ip="192.168.1.50",
                serial="01P00A123456789",
                name="车间 P1S",
                model=PrinterModel.P1S,
                access_code="12345678",
                tile_span=2,
            )
            # 能力声明：真实会话由机型固有能力 + 运行时观测得出，
            # 假会话直接给出等价的一组（网页载荷会用到它）
            self.capabilities = PrinterModel.P1S.capabilities
            self.controls_blocked_reason = ""
            self.status = PrinterStatus(
                mqtt_online=True,
                camera_online=True,
                gcode_state="RUNNING",
                progress=47,
                remaining_minutes=83,
                layer_num=12,
                total_layer_num=240,
                subtask_name="benchy.gcode",
                nozzle_temper=245.0,
                nozzle_target_temper=250.0,
                bed_temper=60.0,
                bed_target_temper=60.0,
                wifi_signal="-58dBm",
                lights={"chamber_light": "on"},
                ams_trays=[AmsTray(ams_id=0, tray_id=0, tray_type="PLA", remain=92, has_rfid=True)],
                hms_items=[HmsItem(attr=0x03000100, code=0x00010001, short_code="HMS_X")],
            )
            self.mqtt_auth_error = False
            self.last_camera_state = "streaming"
            self.last_camera_detail = "视频已连接"
            self.video_backend = "TCP6000"
            self.camera_fps = 1.0

        def snapshot(self):
            from app.bambu.models import PrinterStatus

            return self.status if isinstance(self.status, PrinterStatus) else PrinterStatus()

        @property
        def can_control(self) -> bool:
            return True

        def latest_frame(self):
            return 0, None

    return _FakeSession()


#: `/api/printers` 每台设备的键集合 —— 前端 `page.py` 的契约。
#: **只允许新增**：删键或改键名会让已缓存旧页面的浏览器显示错误。
PRINTER_PAYLOAD_KEYS = frozenset(
    {
        "index",
        "name",
        "ip",
        "model",
        "span",
        "backend",
        "fps",
        "status_text",
        "mqtt_online",
        "camera_online",
        "can_control",
        "controls_blocked_reason",
        "capabilities",
        "state_text",
        "progress",
        "remaining_text",
        "finish_time",
        "task",
        "nozzle",
        "nozzle_target",
        "bed",
        "bed_target",
        "chamber",
        "wifi",
        "wifi_level",
        "light",
        "paused",
        "printing",
        "ams",
        "external",
        "active_tray",
        "hms",
        "layer_text",
        "problem_text",
    }
)

#: 载荷顶层的键集合
TOP_LEVEL_PAYLOAD_KEYS = frozenset(
    {"total", "camera_online", "mqtt_online", "printing", "web_fps", "printers"}
)


def _status_payload() -> dict:
    """不经过网络，直接从处理器取出载荷。

    `WebServer` 是在 `start()` 里把自身挂到 HTTP 服务器的 `.app` 上的，
    `_Handler.app` 属性再去读 `self.server.app`。这里手工补上这层绑定，
    并把处理器实例化绕过 `BaseHTTPRequestHandler`（它会去碰真实的 socket）。
    """
    from app.web import server as web_server

    server = web_server.WebServer(get_sessions=lambda: [_fake_session()], port=0, host="127.0.0.1")
    server.app = server  # type: ignore[attr-defined] - 与 start() 里的绑定等价
    handler = object.__new__(web_server._Handler)
    handler.server = server
    return handler._status_payload()


def test_网页载荷顶层键未变():
    """契约：`/api/printers` 顶层键只增不减。"""
    payload = _status_payload()
    assert set(payload) >= TOP_LEVEL_PAYLOAD_KEYS, (
        f"顶层键缺失：{sorted(TOP_LEVEL_PAYLOAD_KEYS - set(payload))}"
    )


def test_网页载荷每台设备的键未变():
    """契约：每台设备的键只增不减（前端内嵌 JS 无编译期检查，删键会静默失效）。"""
    payload = _status_payload()
    printers = payload["printers"]
    assert printers, "假会话未被载荷收录，测试本身失效"
    keys = set(printers[0])
    assert keys >= PRINTER_PAYLOAD_KEYS, (
        f"设备键缺失：{sorted(PRINTER_PAYLOAD_KEYS - keys)}"
    )


def test_网页载荷的数值已被钳制():
    """契约：进度 0..100、跨度 1..3（前端直接用来排版，越界会画错）。"""
    payload = _status_payload()
    item = payload["printers"][0]
    assert 0 <= item["progress"] <= 100
    assert 1 <= item["span"] <= 3
    assert isinstance(item["ams"], list)
    assert isinstance(item["hms"], list)


# --------------------------------------------------------------------------- 前端与指令


def test_页面DOM标识与按钮动作未变():
    """契约：内嵌 JS 依赖的 DOM 类名与按钮动作名必须保留。

    `app/web/page.py` 是单文件内嵌 HTML+JS，没有构建与类型检查，
    改 DOM 名不会有任何报错，只会让功能静默失效。
    这些是 JS 里 `querySelector` 的选择器，必须与页面元素一致。
    """
    from app.web.page import INDEX_HTML

    for marker in (
        'class="chamber"',
        'class="filament"',
        'class="wifi"',
        'class="finish"',
        'class="hms"',
        'id="hmsbox"',
    ):
        assert marker in INDEX_HTML, f"页面缺少 DOM 标识：{marker}"
    for action in ("pause", "stop", "light"):
        assert f'data-act="{action}"' in INDEX_HTML, f"页面缺少控制按钮：{action}"
    assert "querySelector('button[data-act=\"pause\"]')" in INDEX_HTML, (
        "暂停按钮的选择器变了，JS 会静默失效"
    )
    assert "/api/live" in INDEX_HTML, "多路复用端点名是前端契约"


def test_前端多路复用解析参数未变():
    """契约：前端解析 `/api/live` 记录头用的魔数与长度必须与服务端一致。

    前端把魔数写成两个字节常量（0x42='B'、0x4D='M'），这里同时校验
    前端常量与服务端 `RECORD_MAGIC`，防止只改一边。
    """
    from app.web import server as web_server
    from app.web.page import INDEX_HTML

    assert web_server.RECORD_MAGIC == b"BM"
    assert "MAGIC_A = 0x42" in INDEX_HTML and "MAGIC_B = 0x4D" in INDEX_HTML, (
        "前端多路复用魔数变了"
    )
    assert f"HEADER = {web_server.RECORD_HEADER}" in INDEX_HTML, "前端记录头长度与服务端不一致"
    assert f"KIND_FRAME = {web_server.KIND_FRAME}" in INDEX_HTML
    assert f"KIND_STATUS = {web_server.KIND_STATUS}" in INDEX_HTML


def _control_handler(session):
    """构造一个只够调 `_control()` 的处理器（不碰 socket）。"""
    from app.web import server as web_server

    server = web_server.WebServer(get_sessions=lambda: [session], port=0, host="127.0.0.1")
    server.app = server  # type: ignore[attr-defined]
    handler = object.__new__(web_server._Handler)
    handler.server = server
    return handler


class _RecordingSession:
    """记录被调用的控制方法，用来验证「网页动作 → 会话调用」的映射。"""

    def __init__(self, light_on: bool = False) -> None:
        from app.bambu.models import PrinterStatus

        self.calls: list[tuple] = []
        self._status = PrinterStatus(lights={"chamber_light": "on" if light_on else "off"})

    @property
    def can_control(self) -> bool:
        return True

    def snapshot(self):
        return self._status

    def pause_print(self) -> bool:
        self.calls.append(("pause",))
        return True

    def resume_print(self) -> bool:
        self.calls.append(("resume",))
        return True

    def stop_print(self) -> bool:
        self.calls.append(("stop",))
        return True

    def set_light(self, on: bool) -> bool:
        self.calls.append(("light", on))
        return True

    def set_speed(self, level: int) -> bool:
        self.calls.append(("speed", level))
        return True


@pytest.mark.parametrize(
    ("action", "value", "expected_call"),
    [
        ("pause", "", ("pause",)),
        ("resume", "", ("resume",)),
        ("stop", "", ("stop",)),
        ("light_on", "", ("light", True)),
        ("light_off", "", ("light", False)),
        ("light_toggle", "", ("light", True)),  # 起始是关灯 → 切换为开
        ("speed", "3", ("speed", 3)),
        ("speed", "99", ("speed", 4)),  # 档位钳制到 1..4
        ("speed", "0", ("speed", 1)),
    ],
)
def test_网页控制动作映射到会话调用(action, value, expected_call):
    """契约：网页 `POST /api/command` 的每个动作都必须转成对应的会话方法调用。

    以前这里用源码字符串匹配来锁契约，那样既脆弱又抓不住真错误
    （改名能通过、映射写错不会）。改成真调用，验证的是行为。
    """
    session = _RecordingSession(light_on=False)
    handler = _control_handler(session)
    ok, _detail = handler._control(0, action, value)
    assert ok is True
    assert session.calls == [expected_call], f"{action} 没有转成期望的会话调用"


def test_网页未知动作与越界序号被拒绝():
    """契约：未知动作与越界序号必须返回失败，且不触发任何会话调用。"""
    session = _RecordingSession()
    handler = _control_handler(session)

    ok, detail = handler._control(0, "reboot", "")
    assert ok is False
    assert "未知指令" in detail, "未知指令的文案被测试与前端依赖"

    ok, _detail = handler._control(5, "pause", "")
    assert ok is False
    assert session.calls == [], "越界序号不应触发任何控制调用"


def test_多路复用记录格式未变():
    """契约：`/api/live` 的服务端记录头常量不能改（服务端与前端必须同时改）。

    前端一侧的对应常量由 `test_前端多路复用解析参数未变` 校验。
    """
    from app.web import server as web_server

    assert web_server.RECORD_HEADER == 9
    assert web_server.KIND_FRAME == 1
    assert web_server.KIND_STATUS == 2
    assert web_server.STATUS_INDEX == 0xFFFF


# --------------------------------------------------------------------------- 配置兼容


def test_配置文件字段向后兼容(tmp_path, monkeypatch):
    """契约：**老版本写出的配置文件必须仍能被读入**。

    重构会往 `PrinterInfo` 加字段（family/kind/credential…），但老文件里没有它们，
    读入时必须回退到拓竹默认值，不能让老用户升级后配置"消失"。
    """
    monkeypatch.setenv("BAMBU_MONITOR_CONFIG_DIR", str(tmp_path))
    legacy = {
        "printers": [
            {
                "ip": "192.168.1.50",
                "serial": "01P00A123456789",
                "name": "车间 P1S",
                "model": "P1S",
                "firmware": "01.05.00.00",
                "access_code": "12345678",
                "stream_mode": "auto",
                "tile_span": 2,
                "discovered": True,
            }
        ],
        "columns": 3,
        "last_timeout": 20.0,
        "max_fps": 10.0,
    }
    (tmp_path / "config.json").write_text(json.dumps(legacy), encoding="utf-8")

    from app.bambu.models import PrinterModel
    from app.config import AppConfig

    config = AppConfig.load()
    assert len(config.printers) == 1
    printer = config.printers[0]
    assert printer.ip == "192.168.1.50"
    assert printer.serial == "01P00A123456789"
    assert printer.access_code == "12345678"
    assert printer.model is PrinterModel.P1S
    assert printer.tile_span == 2
    assert config.columns == 3


def _fstring_backslash_lines(source: str) -> list[int]:
    """找出「f-string 的替换字段里带反斜杠」的行号。

    Python **3.12 之前**这是语法错误：
        SyntaxError: f-string expression part cannot include a backslash
    本项目要在 3.10 上跑（安卓版内嵌的就是 3.10），所以不能写这种代码。

    为什么不能靠 ``ast.parse(..., feature_version=(3, 10))``：那条限制在 3.12 是
    **词法/编译期**规则，``feature_version`` 不管它（实测照样通过）。
    所以这里用 AST 反查：把替换字段的表达式 ``unparse`` 出来，看有没有反斜杠。
    """
    import ast as _ast

    offenders: list[int] = []
    tree = _ast.parse(source)
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.FormattedValue):
            continue
        try:
            text = _ast.unparse(node.value)
        except Exception:  # noqa: BLE001 - 反解析失败就别误报
            continue
        if "\\" in text:
            offenders.append(getattr(node, "lineno", 0))
    return offenders


def test_fstring里不能出现只在_3_12_以上才合法的写法():
    """契约：仓库里的代码必须能在 **Python 3.10** 上解析（安卓版就是 3.10）。

    真实踩过的坑：`tools/web_check.py` 里写了
    ``f"... {'id=\\"wall\\"' in html}"`` —— 3.12 起合法，3.10/3.11 直接
    SyntaxError，CI 的 3.10 任务因此挂掉，而本地（3.13）一点问题都看不出来。
    """
    # 自证：这条必须被查出来（反斜杠出现在替换字段内部）
    bad = "x = 1\ny = f\"{'a\\\\' in x}\"\n"
    assert _fstring_backslash_lines(bad) == [2], "自证失败：没查出非法写法"
    assert _fstring_backslash_lines('x = 1\nprint(f"a {x} b\\\\n")\n') == [], (
        "反斜杠在替换字段**外面**是合法的，不该误报"
    )

    offenders: list[str] = []
    for path in sorted(PROJECT_ROOT.rglob("*.py")):
        if set(path.parts) & {".venv", "build", "dist", "dist-onefile", "android", "node_modules"}:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            lines = _fstring_backslash_lines(source)
        except SyntaxError:
            continue  # 语法问题由别的测试负责
        offenders.extend(f"{path.relative_to(PROJECT_ROOT)}:{line}" for line in lines)
    assert not offenders, (
        "这些 f-string 在 3.12 之前是语法错误（安卓版跑 3.10）：" + "、".join(offenders)
    )


def test_文件名与命令入口未被改名():
    """契约：模块路径与入口是命令行、Docker、systemd、打包脚本的外部接口。"""
    for relative in (
        "app/main.py",
        "app/headless.py",
        "app/__main__.py",
        "app/sim/__main__.py",
        "app/web/server.py",
        "app/web/page.py",
        "app/bambu/models.py",
        "app/bambu/printer.py",
        "app/bambu/discovery.py",
        "app/bambu/camera.py",
        "app/bambu/rtsp.py",
        "app/bambu/mqtt_worker.py",
        "app/bambu/probe.py",
        "app/bambu/tlsutil.py",
        "app/bambu/ports.py",
        "app/bambu/timeouts.py",
        "app/sim/simulator.py",
        # 设备无关内核：第三方适配器与界面共同依赖的契约
        "app/core/__init__.py",
        "app/core/capabilities.py",
        "app/core/device.py",
        "app/core/adapter.py",
        "app/core/registry.py",
        # 第三方设备族适配器（已落地的族）
        "app/adapters/__init__.py",
        "app/adapters/moonraker/__init__.py",
        "app/adapters/moonraker/adapter.py",
        "app/adapters/moonraker/ws.py",
        "app/adapters/moonraker/fake.py",
    ):
        assert (PROJECT_ROOT / relative).is_file(), f"模块被移动或改名：{relative}"


def test_模拟器公开符号仍可导入():
    """契约：`app.selftest` 与慢测试都从这些名字导入，不能改名。"""
    from app.sim import simulator

    for name in (
        "SIM_MODELS",
        "SIM_DIR",
        "DEFAULT_ACCESS_CODE",
        "start_simulator",
        "stop_simulator",
        "ensure_qt_initialized",
        "make_server_context",
    ):
        assert hasattr(simulator, name), f"模拟器公开符号缺失：{name}"


def test_帮助文案仍列出关键命令行开关():
    """契约：README 与批处理脚本里写明的开关必须仍然存在。"""
    from app.main import build_parser

    options = {action.dest for action in build_parser()._actions}
    for expected in ("core_test", "sim", "screenshot", "web", "export_config", "ui_selftest"):
        assert expected in options, f"命令行开关缺失：--{expected.replace('_', '-')}"


def test_页面标题仍包含产品名():
    """契约：页面标题与 PWA 名称是用户可见的品牌标识（去品牌化需显式决定）。"""
    from app.web.page import INDEX_HTML

    assert "打印机监控" in INDEX_HTML
    assert re.search(r"<title>[^<]*监控", INDEX_HTML), "页面标题丢了"


# --------------------------------------------------------------------------- 构建脚本


def test_含中文的PowerShell脚本必须带UTF8_BOM():
    """契约：`.ps1` 脚本若含非 ASCII 字符，必须以 UTF-8 **BOM** 开头。

    这条不是洁癖，是踩过的坑：**Windows PowerShell 5.1 读取无 BOM 的 UTF-8 文件时
    会按系统 ANSI 代码页（中文系统是 GBK）解码**，中文字符被拆坏后会连带破坏语法，
    报出一堆 `Unexpected token`，而且**报错行号对不上真实内容** ——
    极容易被误导到错误的方向去排查。

    处理方式（加 BOM）：
        $p = "xxx.ps1"
        $t = [IO.File]::ReadAllText($p, [Text.Encoding]::UTF8)
        [IO.File]::WriteAllText($p, $t, (New-Object Text.UTF8Encoding($true)))
    """
    scripts = sorted(PROJECT_ROOT.glob("*.ps1")) + sorted(PROJECT_ROOT.glob("*/*.ps1"))
    assert scripts, "没有找到任何 .ps1 脚本，测试本身可能失效了"
    for script in scripts:
        raw = script.read_bytes()
        if all(byte < 0x80 for byte in raw):
            continue  # 纯 ASCII 脚本不受影响
        assert raw.startswith(b"\xef\xbb\xbf"), (
            f"{script.relative_to(PROJECT_ROOT)} 含非 ASCII 字符但没有 UTF-8 BOM，"
            "PowerShell 5.1 会按 ANSI 解码并破坏语法"
        )


def test_含here_string的PowerShell脚本必须用CRLF换行():
    """契约：用了 here-string（``@"`` … ``"@``）的 `.ps1` 必须以 CRLF 换行。

    同样是踩过的坑：**PowerShell 5.1 不把「只有 LF 的换行」当作 here-string 的
    结束符**。脚本若整篇是 LF（编辑器/工具重写文件时很常见），会因为找不到
    ``"@`` 而报 `The string is missing the terminator: "@` —— 而这个报错同样
    指向不到真正的原因，只会让人怀疑引号配对。

    PowerShell 7 对 LF 是宽容的，所以只有用 5.1 跑才会暴露。
    """
    scripts = sorted(PROJECT_ROOT.glob("*.ps1")) + sorted(PROJECT_ROOT.glob("*/*.ps1"))
    assert scripts, "没有找到任何 .ps1 脚本，测试本身可能失效了"
    for script in scripts:
        raw = script.read_bytes()
        if b'@"' not in raw:
            continue  # 没有 here-string 就不受影响
        lone_lf = raw.replace(b"\r\n", b"")
        assert b"\n" not in lone_lf, (
            f"{script.relative_to(PROJECT_ROOT)} 含 here-string 但存在只有 LF 的换行，"
            'PowerShell 5.1 会报 The string is missing the terminator: "@'
        )


def test_打包脚本引用的spec文件都存在():
    """契约：打包脚本里点名的 spec 必须真的在仓库里（否则构建时才炸）。"""
    for name in (
        "BambuMonitor-onefile.spec",
        "BambuMonitor-headless.spec",
        "BambuMonitor-linux.spec",
        "BambuMonitor.spec",
    ):
        assert (PROJECT_ROOT / name).is_file(), f"缺少打包配置：{name}"


def test_windowed_spec必须同时产出命令行版():
    """契约：windowed 打包必须配套产出一个 console 版，否则命令行能力全丢。

    踩过的坑：`BambuMonitor-onefile.spec` 用 `console=False`（windowed 引导器），
    而 windowed 引导器**没有控制台句柄** —— `--version` / `--core-test`
    既看不到输出，**重定向和管道拿到的也是空文件**（程序内部把 print 丢进
    os.devnull）。spec 的文档里一直写着"CLI 用法请用同时产出的命令行版"，
    但那个命令行版从来没真正产出过（它指向的 build_packaging.py 并不存在），
    于是所有命令行用法都是静默失效的。

    这条测试盯住两件事：spec 里确实有 console=True 的第二个 EXE，
    而且它在文档里被点名。CI 的冒烟验证也会跑这个 CLI 产物。
    """
    spec = (PROJECT_ROOT / "BambuMonitor-onefile.spec").read_text(encoding="utf-8")

    assert "console=False" in spec, "GUI 版应当保持 windowed（双击不弹控制台窗口）"
    assert "console=True" in spec, (
        "windowed 打包必须另有 console=True 的产物，否则 --version/--core-test "
        "等命令行输出会静默消失（连重定向都是空文件）"
    )
    assert "BambuMonitor-cli" in spec, "命令行版应当命名为 BambuMonitor-cli"

    # 文档必须告诉用户有两个产物，否则等于没产出
    packaging = (PROJECT_ROOT / "docs" / "PACKAGING.md").read_text(encoding="utf-8")
    assert "BambuMonitor-cli" in packaging, "打包文档没说清有两个 exe，用户会踩空"


def test_发布工作流用的exe与实际产出的一致():
    """契约：Release 工作流引用的产物名必须与 spec 产出的名字对得上。

    这条是为了防止"工作流里写了一个不存在的文件名"这类错误 ——
    它只会在真正发版时才暴露，而那时已经晚了。
    """
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    spec = (PROJECT_ROOT / "BambuMonitor-onefile.spec").read_text(encoding="utf-8")

    for name in ("BambuMonitor.exe", "BambuMonitor-cli.exe"):
        assert name in workflow, f"发布工作流没有处理产物 {name}"
    # spec 里 name="BambuMonitor" / name="BambuMonitor-cli" -> 产物即上面两个
    assert 'name="BambuMonitor"' in spec
    assert 'name="BambuMonitor-cli"' in spec


def test_控制被挡的提示不能笼统说成控制不可用():
    """契约：签名要求只挡 `print` 段命令，提示文案不能说成"控制不可用"。

    踩过的坑：状态条的提示原本文案是「⚠ 控制不可用（点此查看原因）」，
    而它的触发条件是 `controls_blocked_reason` 非空 —— 也就是**固件要求签名**。
    但签名只覆盖 `print` 段的暂停/停止/速度，**灯控走 `system` 段、实测可用**。
    于是安卓用户看到"控制不可用"，随手点一下灯却发现能用：提示与事实矛盾，
    比不提示更糟。

    这条测试静态守住三件事（都放在源码上断言，不依赖 node 等外部命令）：
    1. 提示文案里不得再出现笼统的"控制不可用"；
    2. 提示必须**按具体被挡的命令**算（出现 blockedActs 那段逻辑）；
    3. 提示必须写明"开关灯不受影响"这类限定，而不是让用户以为全都不能用。
    """
    from app.web.page import INDEX_HTML

    # 只看真正赋值给提示的那几行；注释里提到历史文案是允许的
    marker = "hint.textContent"
    lines = [ln.strip() for ln in INDEX_HTML.splitlines() if marker in ln]
    assert lines, "页面里找不到给 hint.textContent 赋值的地方"

    for line in lines:
        assert "控制不可用" not in line, (
            "提示文案又变回笼统的「控制不可用」了：" + line
        )

    # 按具体命令算，而不是一刀切
    assert "blockedActs" in INDEX_HTML, (
        "提示应当按具体被挡的命令（暂停/停止）来算，而不是笼统地说控制不可用"
    )
    assert "style.display !== 'none'" in INDEX_HTML, (
        "算被挡命令时要排除当前没显示的按钮，否则空闲时会误报"
    )

    # 必须明确告诉用户"哪部分仍然可用"，否则用户会以为整个控制都坏了
    assert "灯不受影响" in INDEX_HTML or "开关灯不受影响" in INDEX_HTML, (
        "提示里必须写明灯控不受影响 —— 否则用户看到提示会以为灯也不能用"
    )

    # 必须点出**怎么放行**：第三方客户端走局域网接口，登录拓竹账号是没用的，
    # 只有用户自己在打印机上开局域网模式/开发者模式（或用农场管家）才能控制
    assert "局域网模式" in INDEX_HTML, (
        "提示必须告诉用户去开「局域网模式」"
    )
    assert "开发者模式" in INDEX_HTML, (
        "提示必须告诉用户去开「开发者模式」"
    )
    # ⚠️ 这两条是**实机踩出来的**（用户 12 台真机的结论）：
    #   * 只开局域网模式**没用** —— 画面遥测会通，暂停/停止依旧被固件忽略；
    #     必须在打印机上先开局域网模式、再开开发者模式；
    #   * 官方「农场管家 / Farm Manager」拿得到签名密钥，是最省事的路。
    # 早期的文案写成"任选一条放行即可"，把用户引到了没有用的那条路上。
    assert "农场管家" in INDEX_HTML, (
        "必须推荐官方「农场管家」（它不需要开发者模式）"
    )

    # 完整原因（点开提示后显示的 alert）里还要说清"登录账号没用"
    from app.bambu.models import PrinterInfo, PrinterModel
    from app.bambu.printer import PrinterSession

    session = PrinterSession(PrinterInfo(ip="127.0.0.1", model=PrinterModel.A2L))
    assert session.controls_blocked_reason == "", "未收到 fun 时不该有提示"
    session.status.apply_report({"print": {"fun": "100d122002fbd"}})
    assert session.controls_blocked is True, "前提没造出来（需要签名）"

    reason = session.controls_blocked_reason
    assert reason, "需要签名时应当给出原因"
    assert "局域网模式" in reason and "开发者模式" in reason, "原因里要给出可行的放行方式"
    assert "登录" in reason and "账号" in reason, (
        "要明确说清「只登录拓竹账号不够」，否则用户会一直以为是软件坏了"
    )
    assert "灯" in reason, "要说清灯控不受影响"

    # 内嵌 JS 本身必须语法正确（HTML 里的 `<` 等字符容易把脚本写坏）
    if importlib.util.find_spec("esprima") is None:  # pragma: no cover
        pytest.skip("未安装 esprima，跳过内嵌 JS 语法解析")

    import esprima

    script = INDEX_HTML.split("<script>", 1)[-1].rsplit("</script>", 1)[0]
    esprima.parseScript(script)  # 语法错会直接抛


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
