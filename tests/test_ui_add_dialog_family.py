"""契约：「添加 / 编辑打印机」对话框按**设备族**决定字段（凭据 / 端口 / 摄像头）。

为什么单独测这个对话框：接入 Klipper/Moonraker 后，同一个界面要服务两种完全不同的
设备 —— 凭据那一行在拓竹是「访问代码」、在 Moonraker 是「API Key」（且内网可留空），
端口从 8883 变成「80 优先、7125 回退」，机型/视频通道/序列号对 Klipper 机器根本没有意义。
这些差异如果只在界面代码里硬编码，会出现两种真实的坏结果：

* 把 API Key 写进 ``access_code``（拓竹会话会拿它去连 MQTT，必然失败）；
* 切一下设备族就把上一族填好的凭据冲掉（用户刚复制的 Key 没了）。

全部离线：对话框只被**构造**与**读控件**，「测试连接」的线程一律被替换成记录调用，
不会真的连任何设备（本文件不打 ``no_network`` 标记，所以更要用 stub 兜住）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

# ⚠️ 必须在导入 PySide6 **之前**判断：CI 刻意不装 PySide6，
# 而模块级的 ``from PySide6... import`` 在无 Qt 时是收集期错误（不是跳过）。
pytest.importorskip("PySide6.QtWidgets", reason="界面测试需要 PySide6（CI 不装它）")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLineEdit,
    QMessageBox,
)

from app.bambu.models import PrinterInfo  # noqa: E402
from app.core import registry  # noqa: E402
from app.ui.add_dialog import PrinterEditDialog  # noqa: E402

FAMILY_BAMBU = registry.FAMILY_BAMBU
FAMILY_MOONRAKER = registry.FAMILY_MOONRAKER

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def dialog_factory(monkeypatch, isolated_config_dir):
    """造对话框，并把所有模态弹窗挡掉。

    ``_accept()`` 在缺 IP 时会弹 ``QMessageBox.warning``（模态）：offscreen 下没人点，
    pytest 会永久卡住。这里替换成记录调用，测试还顺便能断言"该弹的提示弹了"。
    """
    _app()
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *args, **kwargs: warnings.append(args[2]))
    )
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    created: list[PrinterEditDialog] = []

    def build(printer=None) -> PrinterEditDialog:
        dialog = PrinterEditDialog(None, printer=printer)
        created.append(dialog)
        return dialog

    build.warnings = warnings  # type: ignore[attr-defined]
    yield build
    for dialog in created:
        dialog.close()


def _select_family(dialog: PrinterEditDialog, family: str) -> None:
    """像用户那样切设备族（走 currentIndexChanged，而不是直接改内部状态）。"""
    index = dialog.family_combo.findData(family)
    assert index >= 0, f"设备族下拉里没有 {family}"
    dialog.family_combo.setCurrentIndex(index)


# --------------------------------------------------------------------------- ① 下拉


def test_设备族下拉列出注册表里的全部族(dialog_factory):
    """下拉的选项必须与注册表一致 —— 否则新族注册了却选不到。"""
    dialog = dialog_factory()
    expected = {descriptor.family: descriptor.label for descriptor in registry.all_families()}
    assert expected, "注册表里一个设备族都没有"
    assert dialog.family_combo.count() == len(expected)

    shown = {
        dialog.family_combo.itemData(index): dialog.family_combo.itemText(index)
        for index in range(dialog.family_combo.count())
    }
    assert shown == expected, "下拉的族 id / 展示名与注册表不一致"
    # 新建时默认是拓竹（老配置没有 family 字段 → 一律解析为拓竹）
    assert dialog.family_combo.currentData() == FAMILY_BAMBU


def test_编辑既有设备时下拉停在它自己的族(dialog_factory):
    """编辑一台 Moonraker 设备时，下拉必须预选 Moonraker 而不是拓竹。"""
    dialog = dialog_factory(PrinterInfo(ip="192.168.1.9", family=FAMILY_MOONRAKER))
    assert dialog.family_combo.currentData() == FAMILY_MOONRAKER


def test_不认识的族值退回拓竹而不是崩掉(dialog_factory):
    """配置可能来自更新的版本：未知 family 必须安全退回拓竹。"""
    dialog = dialog_factory(PrinterInfo(ip="192.168.1.9", family="未来的某个族"))
    assert dialog.family_combo.currentData() == FAMILY_BAMBU


# --------------------------------------------------------------------- ② 标签与提示


def test_切到_moonraker_后凭据标签与提示跟着变(dialog_factory):
    """凭据行的文案来自 ``CredentialPolicy``，不是写死的「访问代码」。"""
    dialog = dialog_factory()
    bambu_policy = registry.get(FAMILY_BAMBU).credential
    assert dialog.credential_label.text() == bambu_policy.label
    assert dialog.code_edit.placeholderText() == bambu_policy.hint

    _select_family(dialog, FAMILY_MOONRAKER)
    policy = registry.get(FAMILY_MOONRAKER).credential
    assert dialog.credential_label.text() == "API Key"
    assert dialog.credential_label.text() == policy.label
    assert dialog.code_edit.placeholderText() == policy.hint
    assert dialog.code_edit.echoMode() == QLineEdit.Password

    _select_family(dialog, FAMILY_BAMBU)
    assert dialog.credential_label.text() == "访问代码"


def test_非拓竹族停用拓竹专有行且切回去恢复(dialog_factory):
    """机型 / 视频通道 / 序列号是拓竹专有；Klipper 机器没有这些概念。"""
    dialog = dialog_factory()
    assert dialog.serial_edit.isEnabled() and dialog.model_combo.isEnabled()
    assert dialog.stream_combo.isEnabled() and dialog.auto_serial.isEnabled()
    assert not dialog.bambu_only_note.isVisibleTo(dialog)

    _select_family(dialog, FAMILY_MOONRAKER)
    assert not dialog.serial_edit.isEnabled()
    assert not dialog.model_combo.isEnabled()
    assert not dialog.stream_combo.isEnabled()
    assert not dialog.auto_serial.isEnabled(), "没有序列号可识别，勾选框也该停用"
    assert dialog.bambu_only_note.isVisibleTo(dialog), "停用时要说明原因，否则像界面坏了"

    _select_family(dialog, FAMILY_BAMBU)
    assert dialog.serial_edit.isEnabled() and dialog.model_combo.isEnabled()
    assert dialog.stream_combo.isEnabled() and dialog.auto_serial.isEnabled()


def test_端口提示写明默认端口与候选端口(dialog_factory):
    """端口那行的提示必须说清「留空用哪个端口」以及会依次试哪些端口。"""
    dialog = dialog_factory()
    bambu_hint = dialog.port_edit.placeholderText()
    assert str(registry.get(FAMILY_BAMBU).default_port) in bambu_hint

    _select_family(dialog, FAMILY_MOONRAKER)
    hint = dialog.port_edit.placeholderText()
    moonraker = registry.get(FAMILY_MOONRAKER)
    assert str(moonraker.default_port) in hint
    for port in moonraker.candidate_ports:
        assert str(port) in hint, f"候选端口 {port} 没写进提示"
    # 80 优先、7125 回退这个顺序是 U1 与通用 Klipper 机器的分界，必须体现
    order = " → ".join(str(port) for port in moonraker.candidate_ports)
    assert order in hint, f"候选端口的顺序没写对：{hint}"


# ------------------------------------------------------------------------ ③④ 保存


def test_moonraker_保存时凭据写进_api_key而不是_access_code(dialog_factory):
    """红线：API Key **绝不能**写进 ``access_code``。"""
    dialog = dialog_factory()
    dialog.ip_edit.setText("192.168.1.77")
    _select_family(dialog, FAMILY_MOONRAKER)
    dialog.code_edit.setText("SECRET-API-KEY")
    dialog.port_edit.setText("7125")
    dialog.camera_edit.setText("http://192.168.1.77/camera/monitor.jpg")

    dialog._accept()
    info = dialog.result
    assert info is not None, "填了 IP 就该保存成功"
    assert info.family == FAMILY_MOONRAKER
    assert info.api_key == "SECRET-API-KEY"
    assert info.access_code == "", "API Key 被写进拓竹字段了"
    assert info.port == 7125
    assert info.camera_url == "http://192.168.1.77/camera/monitor.jpg"


def test_拓竹保存时凭据仍写进_access_code(dialog_factory):
    """反过来也要成立：拓竹的访问代码不能跑到 api_key 里。"""
    dialog = dialog_factory()
    dialog.ip_edit.setText("192.168.1.50")
    dialog.code_edit.setText("12345678")
    dialog._accept()
    assert dialog.result.access_code == "12345678"
    assert dialog.result.api_key == ""
    assert dialog.result.family == "", "拓竹写空 family（老配置的约定）"
    assert dialog.result.serial == "" and dialog.result.port == 0


@pytest.mark.parametrize("text", ["", "0", "   "])
def test_端口留空或零表示用族默认端口(dialog_factory, text):
    """留空 / 0 = 跟着族默认端口走（存 0，不展开成具体端口）。"""
    dialog = dialog_factory()
    dialog.ip_edit.setText("192.168.1.77")
    _select_family(dialog, FAMILY_MOONRAKER)
    dialog.port_edit.setText(text)

    dialog._accept()
    assert dialog.result is not None
    assert dialog.result.port == 0
    # 实际用的端口由注册表决定：组件从配置里读到 0 时会退到族默认端口
    assert registry.default_port(dialog.result) == registry.get(FAMILY_MOONRAKER).default_port
    assert registry.default_port(PrinterInfo()) == registry.get(FAMILY_BAMBU).default_port


def test_缺少_ip_时不保存且给提示(dialog_factory):
    """校验路径：没有 IP 就不该产出 result（否则会往配置里塞一台空机器）。"""
    dialog = dialog_factory()
    _select_family(dialog, FAMILY_MOONRAKER)
    dialog._accept()
    assert dialog.result is None
    assert dialog_factory.warnings, "至少要给用户一句提示"


# --------------------------------------------------------------------------- ⑤ 回填


def test_编辑既有_moonraker_设备时回填_family_api_key_port(dialog_factory):
    """回填：编辑一台已保存的 Moonraker 设备，控件里应当是它原来的值。"""
    printer = PrinterInfo(
        ip="192.168.1.88",
        name="车间 U1",
        family=FAMILY_MOONRAKER,
        api_key="OLD-API-KEY",
        port=80,
        camera_url="http://192.168.1.88/camera/monitor.jpg",
    )
    dialog = dialog_factory(printer)
    assert dialog.family_combo.currentData() == FAMILY_MOONRAKER
    assert dialog.code_edit.text() == "OLD-API-KEY"
    assert dialog.port_edit.text() == "80"
    assert dialog.camera_edit.text() == printer.camera_url
    assert dialog.ip_edit.text() == "192.168.1.88"
    # 这台设备没有拓竹那套概念，那几行应当已经是停用状态
    assert not dialog.serial_edit.isEnabled()
    assert not dialog.model_combo.isEnabled()

    # 不动任何控件直接保存：不应丢字段，也不应把值串到别的字段上
    dialog._accept()
    result = dialog.result
    assert result is not None
    assert (result.family, result.api_key, result.port) == (FAMILY_MOONRAKER, "OLD-API-KEY", 80)
    assert result.camera_url == printer.camera_url
    assert result.access_code == "" and result.serial == ""


def test_编辑既有拓竹设备时回填访问代码(dialog_factory):
    """老配置（没有 family 字段、只有 access_code）编辑时同样要回填正确。"""
    printer = PrinterInfo(ip="192.168.1.50", name="车间 A1", access_code="12345678")
    dialog = dialog_factory(printer)
    assert dialog.family_combo.currentData() == FAMILY_BAMBU
    assert dialog.code_edit.text() == "12345678"
    assert dialog.port_edit.text() == "", "port=0（族默认）时输入框应为空"
    assert dialog.serial_edit.isEnabled()


# ----------------------------------------------------------------- 每个族各留一份凭据


def test_切换设备族时各自保留凭据(dialog_factory):
    """切族不能互相冲掉：这是"填完 Moonraker 再切回拓竹，代码还在"的保证。"""
    dialog = dialog_factory()
    dialog.code_edit.setText("12345678")  # 拓竹的访问代码
    _select_family(dialog, FAMILY_MOONRAKER)
    assert dialog.code_edit.text() == "", "换族后不能把访问代码当成 API Key"

    dialog.code_edit.setText("MOON-API-KEY")
    _select_family(dialog, FAMILY_BAMBU)
    assert dialog.code_edit.text() == "12345678", "切回拓竹后访问代码丢了"

    _select_family(dialog, FAMILY_MOONRAKER)
    assert dialog.code_edit.text() == "MOON-API-KEY", "再切回 Moonraker 后 API Key 丢了"

    # 各自保存、互不串字段
    dialog.ip_edit.setText("192.168.1.77")
    dialog._accept()
    assert (dialog.result.api_key, dialog.result.access_code) == ("MOON-API-KEY", "")

    other = dialog_factory()
    other.ip_edit.setText("192.168.1.50")
    other.code_edit.setText("87654321")
    other._accept()
    assert (other.result.access_code, other.result.api_key) == ("87654321", "")


# ---------------------------------------------------------------------- 测试连接


def test_拓竹族的测试连接仍用原探测线程并拦空访问代码(dialog_factory, monkeypatch):
    """拓竹这一路的行为不能变：缺访问代码先提示，不启动线程；有则用 _ProbeThread。"""
    from app.ui import add_dialog

    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *args, **kwargs: warnings.append(args[2]))
    )
    starts: list[object] = []
    monkeypatch.setattr(add_dialog._ProbeThread, "start", lambda self: starts.append(self))

    dialog = dialog_factory()
    dialog.ip_edit.setText("192.168.1.5")
    dialog._start_test()
    assert starts == [], "缺访问代码时不该真的开线程"
    assert warnings and "访问代码" in warnings[0]

    dialog.code_edit.setText("12345678")
    dialog._start_test()
    assert len(starts) == 1
    assert isinstance(starts[0], add_dialog._ProbeThread)
    assert starts[0]._serial == ""  # 没有序列号就交给探测自动识别


def test_非拓竹族的测试连接走_http_探测且凭据可留空(dialog_factory, monkeypatch):
    """第三方族用 HTTP 只读探测；Moonraker 内网免鉴权，所以空凭据不该被拦。"""
    from app.ui import add_dialog

    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *args, **kwargs: warnings.append(args[2]))
    )
    starts: list[object] = []
    monkeypatch.setattr(add_dialog._HttpProbeThread, "start", lambda self: starts.append(self))

    dialog = dialog_factory()
    dialog.ip_edit.setText("192.168.1.88")
    _select_family(dialog, FAMILY_MOONRAKER)
    dialog._start_test()
    assert warnings == [], "Moonraker 的 API Key 是可选的，不该弹提示"
    assert len(starts) == 1
    assert isinstance(starts[0], add_dialog._HttpProbeThread)
    # 没填端口 → 按族的候选顺序试（80 优先、7125 回退）
    assert starts[0]._ports == list(registry.get(FAMILY_MOONRAKER).candidate_ports)


def test_填了端口就只探测该端口(dialog_factory, monkeypatch):
    """用户明确填了端口，就不该再去试别的端口（避免误连到别的服务）。"""
    from app.ui import add_dialog

    starts: list[object] = []
    monkeypatch.setattr(add_dialog._HttpProbeThread, "start", lambda self: starts.append(self))

    dialog = dialog_factory()
    dialog.ip_edit.setText("192.168.1.88")
    _select_family(dialog, FAMILY_MOONRAKER)
    dialog.port_edit.setText("7125")
    dialog._start_test()
    assert starts[0]._ports == [7125]


def test_http_探测打印_klipper_与_moonraker_版本(monkeypatch):
    """探测的取值与文案：URL、5 秒超时、X-Api-Key 头、版本行。

    把 ``urlopen`` 换掉，因此**不联网**；这同时钉住了"探测是只读 GET"这件事。
    """
    from app.ui import add_dialog

    class _FakeResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

    seen: dict = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["timeout"] = timeout
        seen["api_key"] = request.get_header("X-api-key")
        return _FakeResponse(
            json.dumps(
                {
                    "result": {
                        "moonraker_version": "1.4.1-fake",
                        "klippy_state": "ready",
                        "klippy_connected": True,
                        "components": ["klippy_apis", "webcam"],
                    }
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr(add_dialog.urllib.request, "urlopen", fake_urlopen)

    payload = add_dialog._fetch_server_info(
        "192.168.1.88", 80, "K", add_dialog.PROBE_HTTP_TIMEOUT
    )
    assert seen["url"] == "http://192.168.1.88:80/server/info"
    assert seen["method"] == "GET", "探测必须是只读的 GET"
    assert seen["timeout"] == 5.0
    assert seen["api_key"] == "K"

    lines = "\n".join(add_dialog._describe_server_info(80, payload))
    assert "1.4.1-fake" in lines, "没打印 Moonraker 版本"
    assert "就绪" in lines, "klippy 状态应翻成中文"
    assert "8883" not in lines and "6000" not in lines, (
        "第三方族的结论里不该出现拓竹的端口体系（会误导用户）"
    )


def test_http_探测失败时的结论可读():
    """端口全不通时也要给出可读结论，而不是空白日志。"""
    from app.ui import add_dialog

    lines = add_dialog._describe_server_info(7125, {})
    assert any("成功" in line for line in lines)
    assert not any("版本" in line for line in lines), "没有版本信息就不该编一个出来"


# --------------------------------------------------------- 关掉对话框时线程仍在跑

#: 「HTTP 探测线程还在跑时关掉对话框」的用例代码。
#: 为什么必须放子进程：运行中的 QThread 被析构会让 Qt **直接 fail-fast**
#: （Windows 上退出码 0xC0000409），整个 pytest 进程会消失，看起来像随机崩溃。
#: 完整理由与同样的手法见 `tests/test_ui_thread_shutdown.py`。
CLOSE_WHILE_HTTP_PROBING = """
import os, sys, time
sys.path.insert(0, {root!r})
from PySide6.QtWidgets import QApplication
from app.ui import add_dialog

# 全程离线：把 HTTP 实现换成「睡 3 秒再返回空结果」，一个 socket 都不开
def _slow_fetch(*args, **kwargs):
    time.sleep(3)
    return {{}}

add_dialog._fetch_server_info = _slow_fetch

app = QApplication([])
dialog = add_dialog.PrinterEditDialog()
dialog.ip_edit.setText("192.0.2.1")      # 只是界面上的文本，不会真的连
dialog.family_combo.setCurrentIndex(dialog.family_combo.findData("moonraker"))
dialog._start_test()                     # 起线程（会睡 3 秒）
dialog.close()                           # 线程还在跑就关掉：这里不能崩
app.processEvents()
time.sleep(1.0)
print("SURVIVED", flush=True)
"""

#: Windows 下 Qt fail-fast 的退出码（STATUS_STACK_BUFFER_OVERRUN）
FAIL_FAST = 0xC0000409


def test_关闭对话框时_http_探测线程不能随对话框销毁(tmp_path):
    """新增的 HTTP 探测线程同样要走退休名单，否则关窗就把进程带走。"""
    script = tmp_path / "probe_close.py"
    script.write_text(CLOSE_WHILE_HTTP_PROBING.format(root=str(PROJECT_ROOT)), encoding="utf-8")
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["BAMBU_MONITOR_CONFIG_DIR"] = str(tmp_path / "config")
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )
    assert result.returncode != FAIL_FAST, (
        f"关掉对话框时 HTTP 探测线程还在跑，进程 fail-fast（{result.returncode:#x}）：\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "SURVIVED" in result.stdout
