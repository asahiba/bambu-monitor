"""端到端慢测试：内置模拟器 + 真实 TCP/TLS 端口（``app/sim/simulator.py``）。

这是覆盖「I/O 与接口层」最有价值的一条链路：模拟器在 **127.0.0.x 回环地址**上
提供了完整的 MQTT over TLS（8883）、6000 端口 JPEG 视频流与 UDP 发现响应，
因此可以在**完全离线**的前提下验证：

* 6000 端口能取到一帧真 JPEG（以 ``\\xff\\xd8`` 开头）
* MQTT 遥测能连上，并收到含 ``print`` 的报文（进度 / 喷嘴温度 / 热床温度字段齐全）
* ``probe_printer`` 的端到端探测（遥测 + 画面）能成功
* 访问代码错误时取不到画面（模拟器的鉴权真的在生效）

默认不跑（否则会拖慢常规套件），需要显式开启：

    $env:BAMBU_RUN_SLOW="1"; .venv\\Scripts\\python.exe -m pytest tests/test_simulator_e2e.py -v

⚠️ 所有连接目标都取自 ``start_simulator()`` 返回的回环地址（``127.0.0.2``），
绝不会有任何一台真实打印机被连上。
"""

from __future__ import annotations

import os
import socket
import sys
import time

# 必须在导入 PySide6 之前设置：模拟器绘制画面需要 QGuiApplication（无显示环境）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest

from app.bambu.camera import JPEG_SOI, CameraStream, grab_single_frame
from app.bambu.models import PrinterStatus
from app.bambu.mqtt_worker import MqttWorker
from app.bambu.probe import probe_printer
from app.sim.simulator import DEFAULT_ACCESS_CODE, start_simulator, stop_simulator

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("BAMBU_RUN_SLOW") != "1",
        reason="需要 BAMBU_RUN_SLOW=1 才跑端到端慢测试",
    ),
]

ACCESS_CODE = DEFAULT_ACCESS_CODE

#: 导入期先抓住「真」socket 方法：conftest.py 的 no_network 夹具会在测试期把它们换掉
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_BIND = socket.socket.bind
_REAL_LISTEN = socket.socket.listen
_REAL_CREATE_CONNECTION = socket.create_connection


def _is_loopback(address) -> bool:
    host = address[0] if isinstance(address, tuple) and address else ""
    host = str(host)
    return host in ("", "localhost", "::1") or host.startswith("127.")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """覆盖 tests/conftest.py 里的同名 autouse 夹具。

    本文件是端到端链路测试，必须使用 127.0.0.x 回环套接字；conftest 的全局
    no_network 会拦掉一切 bind/connect。这里换成一份更窄的守卫：**仍然禁止任何
    非回环 TCP 连接**（真实打印机与公网都碰不到），只放行回环地址与本地监听。

    注意：模拟器的发现响应器按实现绑定 ``("", 1990/2021)``（0.0.0.0），因此 bind
    必须放行；但本文件从不发送 M-SEARCH / M99999，不会有任何报文离开本机。
    """

    def tcp_guard(original, name):
        def wrapper(self, address, *args, **kwargs):
            is_tcp = getattr(self, "type", None) == socket.SOCK_STREAM
            if is_tcp and not _is_loopback(address):
                raise RuntimeError(f"测试禁止连接非回环 TCP 地址（{name} {address!r}）")
            return original(self, address, *args, **kwargs)

        return wrapper

    def create_connection_guard(address, *args, **kwargs):
        if not _is_loopback(address):
            raise RuntimeError(f"测试禁止连接非回环地址（create_connection {address!r}）")
        return _REAL_CREATE_CONNECTION(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", tcp_guard(_REAL_CONNECT, "connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", tcp_guard(_REAL_CONNECT_EX, "connect_ex"))
    monkeypatch.setattr(socket, "create_connection", create_connection_guard)
    # 无论 conftest 里的联网守卫叫什么名字，模拟器都需要真实的本地监听能力
    monkeypatch.setattr(socket.socket, "bind", _REAL_BIND)
    monkeypatch.setattr(socket.socket, "listen", _REAL_LISTEN)


def wait_for(predicate, timeout: float = 25.0, interval: float = 0.2) -> bool:
    """轮询等待条件成立，超时返回最后一次结果。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


@pytest.fixture(scope="module")
def simulator():
    """启动 1 台虚拟打印机（127.0.0.2），模块结束**必定**停掉，避免残留端口占用。"""
    printers, responder = start_simulator(1, ACCESS_CODE)
    try:
        # 等端口就绪：模拟器的监听线程是异步启动的
        assert printers, "模拟器至少要启动一台虚拟打印机"
        printer = printers[0]
        assert wait_for(lambda: _port_open(printer.ip, 8883), timeout=10.0), "8883 未就绪"
        assert wait_for(lambda: _port_open(printer.ip, 6000), timeout=10.0), "6000 未就绪"
        yield printers
    finally:
        stop_simulator(printers, responder)


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """本机端口是否已监听（只连回环地址）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _printer(simulator):
    printer = simulator[0]
    assert printer.ip.startswith("127."), f"模拟器必须绑在回环地址，实际 {printer.ip}"
    return printer


# ------------------------------------------------------------------ 启动与绑定
def test_模拟器只绑回环地址(simulator):
    """契约：内置模拟器的虚拟打印机全部落在 127.0.0.x，不会占用真实网段。"""
    printer = _printer(simulator)
    assert printer.ip == "127.0.0.2"
    assert printer.serial == "01P00A123456789"
    assert printer.model == "P1S"
    assert printer.access_code == ACCESS_CODE
    assert _port_open(printer.ip, 8883) is True, "MQTT 8883 应当已监听"
    assert _port_open(printer.ip, 6000) is True, "摄像头 6000 应当已监听"


# ------------------------------------------------------------------ 视频链路
def test_6000端口能取到JPEG画面(simulator):
    """契约：6000 端口 TLS + 80 字节鉴权后能取到一帧真 JPEG（``\\xff\\xd8`` 开头），
    并且流状态进入 streaming。"""
    printer = _printer(simulator)
    stream = CameraStream(
        host=printer.ip,
        access_code=ACCESS_CODE,
        serial=printer.serial,
        name="e2e",
    )
    stream.start()
    try:
        frame = stream.wait_first_frame(25.0)
        assert frame is not None, f"未取到画面，状态={stream.state} 详情={stream.detail}"
        assert frame.startswith(JPEG_SOI), "JPEG 必须以 \\xff\\xd8 开头"
        assert len(frame) > 512, "一帧画面不应该只有几个字节"
        assert stream.state == CameraStream.STATE_STREAMING
        assert stream.frame_count >= 1
        seq, latest = stream.latest_frame()
        assert seq >= 1 and latest is not None and latest.startswith(JPEG_SOI)
    finally:
        stream.stop()
        stream.join(timeout=5.0)


def test_错误访问代码取不到画面(simulator):
    """契约：模拟器的 6000 端口鉴权真的生效 —— 访问代码错误时拿不到任何画面。"""
    printer = _printer(simulator)
    frame = grab_single_frame(printer.ip, "00000000", serial=printer.serial, timeout=3.0)
    assert frame is None


def test_grab_single_frame能取到一帧(simulator):
    """契约：grab_single_frame() 是同步抓帧入口（「测试连接」用的就是它）。"""
    printer = _printer(simulator)
    frame = grab_single_frame(
        printer.ip, ACCESS_CODE, serial=printer.serial, timeout=25.0
    )
    assert frame is not None and frame.startswith(JPEG_SOI)


# ------------------------------------------------------------------ 遥测链路
def test_MQTT遥测能收到print报文(simulator):
    """契约：MQTT over TLS(8883) 能连上并收到 ``device/<序列号>/report`` 报文，
    报文含 ``print`` 且带进度 / 喷嘴温度 / 热床温度字段。"""
    printer = _printer(simulator)
    reports: list[dict] = []
    states: list[tuple[str, str]] = []

    worker = MqttWorker(
        host=printer.ip,
        serial=printer.serial,
        access_code=ACCESS_CODE,
        on_report=reports.append,
        on_state=lambda state, detail: states.append((state, detail)),
        name="e2e",
    )
    worker.start()
    try:
        assert wait_for(lambda: bool(reports), timeout=25.0), (
            f"25 秒内没收到任何遥测报文，状态={worker.state} 详情={worker.detail}"
        )
        payload = reports[0]
        assert "print" in payload, "报文必须含 print 段"
        body = payload["print"]
        for key in (
            "gcode_state",
            "mc_percent",
            "nozzle_temper",
            "nozzle_target_temper",
            "bed_temper",
            "bed_target_temper",
        ):
            assert key in body, f"print 段缺少字段 {key}"
        assert 0 <= body["mc_percent"] <= 100
        assert body["nozzle_temper"] >= 0
        assert body["gcode_state"] in ("RUNNING", "PAUSE", "IDLE")

        assert wait_for(lambda: worker.state == MqttWorker.STATE_ONLINE, timeout=10.0)
        assert any(state == MqttWorker.STATE_ONLINE for state, _ in states)
    finally:
        worker.stop()


def test_遥测报文能被PrinterStatus解析(simulator):
    """契约：模拟器报文可以直接喂给 PrinterStatus.apply_report 并解析出打印中状态。"""
    printer = _printer(simulator)
    status = PrinterStatus()
    reports: list[dict] = []
    worker = MqttWorker(
        host=printer.ip,
        serial=printer.serial,
        access_code=ACCESS_CODE,
        on_report=reports.append,
        name="e2e",
    )
    worker.start()
    try:
        assert wait_for(lambda: bool(reports), timeout=25.0)
        assert status.apply_report(reports[0]) is True
        assert status.gcode_state in ("RUNNING", "PAUSE", "IDLE")
        assert status.is_printing is True, "模拟器默认在打印中"
        assert status.remaining_minutes >= 0
        assert status.state_text in ("打印中", "已暂停", "空闲")
        assert status.nozzle_target_temper > 0
        assert status.bed_target_temper > 0
        assert status.ams_trays, "模拟器应当报送 AMS 槽位"
        assert status.active_tray is not None
    finally:
        worker.stop()


# ------------------------------------------------------------------ 端到端探测
def test_probe_printer端到端通过(simulator):
    """契约：probe_printer() 对模拟打印机应当遥测与画面双通，并给出可读摘要。"""
    printer = _printer(simulator)
    steps: list[str] = []
    result = probe_printer(
        printer.ip,
        ACCESS_CODE,
        serial=printer.serial,
        timeout=15.0,
        on_step=steps.append,
    )

    assert result.mqtt_ok is True, f"遥测未通过：{result.mqtt_error}"
    assert result.camera_ok is True, f"画面未通过：{result.camera_error}"
    assert result.ok is True
    assert result.frame is not None and result.frame.startswith(JPEG_SOI)
    assert result.serial == printer.serial
    assert result.model.label == "P1S"
    assert result.status is not None
    assert 0 <= result.status.progress <= 100
    assert result.status.nozzle_temper >= 0
    assert steps, "on_step 应当收到探测过程中的每一步提示"
    summary = result.summary()
    assert "遥测（8883）：成功" in summary
    assert "画面（6000）：成功" in summary
