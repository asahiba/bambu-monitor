"""通道诊断的共用实现：CLI（`tools/diagnose.py`）与界面「通道诊断」对话框都用它。

## 为什么要有这一层

这两个入口原来是**同一套诊断流程的两份实现** —— 端口连通性、TLS 参数、
6000 取帧、RTSPS DESCRIBE、MQTT 探测各写了一遍，连文案都开始漂移：

* CLI 的 ④ 会依次试 `/streaming/live/1`、`/streaming/live/2`、`/live/1` 三个路径，
  而对话框只试一个 —— 于是「命令行能诊断出来、界面上说不行」；
* 两边对 RTSPS 拉流的处理也不同（CLI 试一次真拉流，对话框只做 DESCRIBE）。

现在流程只有一份：本模块提供**只读探测**（返回结构化的 dataclass）与**渲染**
（把结果转成给人看的行）。两个入口都只是「跑一遍、把行打出来」，
所以它们能给出的结论必然一致 —— 这一点由 `tests/test_diagnostics.py` 守着。

## 设计约定

* 探测函数**只读**：不发控制指令、不改配置，可以对着真机跑；
* 每个探测都**不抛异常**（网络问题是被诊断的对象，不是错误），
  失败信息放在返回值里；
* 渲染函数是纯函数（吃 dataclass，吐 ``list[str]``），方便测试与两处复用；
* 需要取消时传 ``should_stop``（对话框关闭时用），在步骤边界立刻收工。
"""

from __future__ import annotations

import base64
import socket
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

from . import tlsutil
from .camera import CameraStream
from .ports import CAMERA_PORT, MQTT_PORT, RTSP_PORT
from .probe import probe_printer
from .timeouts import (
    CAMERA_FIRST_FRAME_TIMEOUT,
    CAMERA_STREAM_JOIN,
    DIAG_RTSP_READ_TIMEOUT,
    DIAG_RTSP_TLS_TIMEOUT,
    DIAG_TCP_TIMEOUT,
    DIAG_TLS_TIMEOUT,
    PROBE_MQTT_TIMEOUT,
    RTSP_ADOPT_TIMEOUT,
)

#: 三个候选端口与展示名（顺序就是展示顺序：先遥测、再 A1/P1 的主通道、最后 X1 系）
CANDIDATE_PORTS: tuple[tuple[int, str], ...] = (
    (MQTT_PORT, "MQTT 遥测"),
    (CAMERA_PORT, "JPEG 画面"),
    (RTSP_PORT, "RTSPS 画面"),
)

#: RTSPS 的候选路径。不同固件用的路径不一样，逐个试才知道哪条是活的。
RTSP_PATHS: tuple[str, ...] = ("/streaming/live/1", "/streaming/live/2", "/live/1")

#: 各步骤的名字（CLI 与对话框共用；序号由 :func:`run` 按实际顺序加，
#: 这样「界面只跑前四步」时不会出现 ①②③④⑥ 这种跳号）
SECTION_PORTS = "端口连通性"
SECTION_TLS = "TLS 参数（证书链 + 安全级别）"
SECTION_CAMERA = "6000 端口画面（TLS + 80 字节鉴权包 + JPEG 帧）"
SECTION_RTSP_DESCRIBE = "RTSPS(322) DESCRIBE"
SECTION_RTSP_FRAME = "RTSPS(322) 实际拉流（OpenCV/FFmpeg）"
SECTION_MQTT = "MQTT 遥测"

#: 序号用的带圈数字
_NUMBERS = "①②③④⑤⑥⑦⑧⑨⑩"


# --------------------------------------------------------------------------- 结构化结果


@dataclass
class PortProbe:
    port: int
    label: str
    ok: bool = False
    error: str = ""


@dataclass
class TlsProbe:
    port: int
    ok: bool = False
    verified: bool = False
    error: str = ""


@dataclass
class CameraProbe:
    ok: bool = False
    frame_bytes: int = 0
    seconds: float = 0.0
    state: str = ""
    detail: str = ""


@dataclass
class RtspDescribeProbe:
    #: 是否成功建立 TLS（服务是否在监听）
    connected: bool = False
    verified: bool = False
    #: 拿到 200 的路径（能直接拉流）
    ok_path: str = ""
    #: 试过的每个路径 -> 状态行
    attempts: list[tuple[str, str]] = field(default_factory=list)
    #: 200 响应里挑出来的 SDP 关键行
    sdp_lines: list[str] = field(default_factory=list)
    #: 看到的 401/403（服务活着但口令没被接受）
    unauthorized: bool = False
    error: str = ""


@dataclass
class RtspFrameProbe:
    #: 本机有没有 OpenCV（没有就整体跳过）
    available: bool = True
    ok: bool = False
    frame_bytes: int = 0
    seconds: float = 0.0
    state: str = ""
    detail: str = ""
    path: str = ""


@dataclass
class MqttProbe:
    ok: bool = False
    lines: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class SectionStart:
    """:func:`run` 的第一个事件：某一步开始了（此时还没做任何探测）。

    界面上需要「先显示标题、再显示结果」—— 否则像「正在连接 MQTT…」这类
    进度行会跑到它自己的标题前面去。
    """

    title: str


@dataclass
class SectionLines:
    """:func:`run` 的第二个事件：这一步的输出行（此时探测已经做完）。"""

    lines: list[str]


#: :func:`run` 产出的事件
SectionEvent = SectionStart | SectionLines


# --------------------------------------------------------------------------- 探测（只读）


def check_ports(
    host: str,
    ports: tuple[tuple[int, str], ...] = CANDIDATE_PORTS,
    timeout: float = DIAG_TCP_TIMEOUT,
) -> list[PortProbe]:
    """逐个端口的 TCP 连通性（只 connect，不发任何数据）。"""
    results: list[PortProbe] = []
    for port, label in ports:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                results.append(PortProbe(port=port, label=label, ok=True))
        except OSError as exc:
            results.append(
                PortProbe(port=port, label=label, ok=False, error=f"{exc.__class__.__name__}: {exc}")
            )
    return results


def check_tls(host: str, ports: tuple[int, ...] | None = None, timeout: float = DIAG_TLS_TIMEOUT) -> list[TlsProbe]:
    """单独做一次 TLS 握手，确认证书链与安全级别是否可用。"""
    targets = ports if ports is not None else tuple(port for port, _ in CANDIDATE_PORTS)
    results: list[TlsProbe] = []
    for port in targets:
        try:
            sock, verified = tlsutil.connect_tls(host, port, timeout=timeout)
            sock.close()
            results.append(TlsProbe(port=port, ok=True, verified=bool(verified)))
        except Exception as exc:  # noqa: BLE001 - 握手失败正是要诊断的现象
            results.append(
                TlsProbe(port=port, ok=False, error=f"{exc.__class__.__name__}: {exc}")
            )
    return results


def grab_camera_frame(
    host: str,
    access_code: str,
    serial: str = "",
    timeout: float = CAMERA_FIRST_FRAME_TIMEOUT,
    should_stop: Optional[Callable[[], bool]] = None,
) -> CameraProbe:
    """走 6000 端口的 TLS + 80 字节鉴权包 + JPEG 帧格式，取一帧。"""
    started = time.time()
    stream = CameraStream(host, access_code, serial=serial)
    stream.start()
    try:
        frame = stream.wait_first_frame(timeout, should_stop=should_stop)
        return CameraProbe(
            ok=bool(frame),
            frame_bytes=len(frame or b""),
            seconds=time.time() - started,
            state=stream.state,
            detail=stream.detail,
        )
    finally:
        stream.stop()
        stream.join(timeout=CAMERA_STREAM_JOIN)


def describe_rtsp(
    host: str,
    access_code: str,
    paths: tuple[str, ...] = RTSP_PATHS,
    *,
    tls_timeout: float = DIAG_RTSP_TLS_TIMEOUT,
    read_timeout: float = DIAG_RTSP_READ_TIMEOUT,
) -> RtspDescribeProbe:
    """对 322 端口逐个路径发 RTSP DESCRIBE，判断服务是否开启、鉴权是否被接受。"""
    probe = RtspDescribeProbe()
    auth = base64.b64encode(f"bblp:{access_code}".encode()).decode()
    try:
        tls, verified = tlsutil.connect_tls(host, RTSP_PORT, timeout=tls_timeout, server_hostname=host)
    except Exception as exc:  # noqa: BLE001
        probe.error = f"{exc.__class__.__name__}: {exc}"
        return probe
    probe.connected = True
    probe.verified = bool(verified)
    try:
        for index, path in enumerate(paths):
            response = _describe_once(
                tls, host, path, cseq=index + 1, auth=auth, read_timeout=read_timeout
            )
            head = response.splitlines()[0] if response else "（无响应）"
            probe.attempts.append((path, head))
            if "200" in head:
                probe.ok_path = path
                probe.sdp_lines = [
                    line.strip()
                    for line in response.splitlines()
                    if line.lower().startswith(("m=", "a=control", "a=rtpmap"))
                ]
                break
            if "401" in head or "403" in head:
                probe.unauthorized = True
                break
    finally:
        try:
            tls.close()
        except OSError:
            pass
    return probe


def _describe_once(
    tls: socket.socket, host: str, path: str, *, cseq: int, auth: str, read_timeout: float
) -> str:
    lines = [
        f"DESCRIBE rtsps://{host}:{RTSP_PORT}{path} RTSP/1.0",
        f"CSeq: {cseq}",
        "User-Agent: bambu-monitor-diag",
        "Accept: application/sdp",
        f"Authorization: Basic {auth}",
    ]
    tls.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    tls.settimeout(read_timeout)
    data = b""
    try:
        while b"\r\n\r\n" not in data:
            chunk = tls.recv(4096)
            if not chunk:
                break
            data += chunk
    except (socket.timeout, TimeoutError, OSError):
        pass
    return data.decode("utf-8", "replace")


def grab_rtsp_frame(
    host: str, access_code: str, timeout: float = RTSP_ADOPT_TIMEOUT
) -> RtspFrameProbe:
    """真拉一次流（OpenCV/FFmpeg）。没装 OpenCV 时 ``available=False``，不算失败。"""
    from .rtsp import RtspStream

    if not RtspStream.available():
        return RtspFrameProbe(available=False)
    started = time.time()
    stream = RtspStream(host, access_code, name="diag")
    stream.start()
    try:
        frame = stream.wait_first_frame(timeout)
        return RtspFrameProbe(
            ok=bool(frame),
            frame_bytes=len(frame or b""),
            seconds=time.time() - started,
            state=stream.state,
            detail=stream.detail,
            path=stream.active_path,
        )
    finally:
        stream.stop()


def probe_mqtt(
    host: str,
    access_code: str,
    serial: str = "",
    timeout: float = PROBE_MQTT_TIMEOUT,
    on_step: Optional[Callable[[str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> MqttProbe:
    """走一遍遥测（8883，不读画面），拿回给人看的结论行。"""
    result = probe_printer(
        host,
        access_code,
        serial=serial,
        timeout=timeout,
        check_camera=False,
        on_step=on_step,
        should_stop=should_stop,
    )
    return MqttProbe(ok=result.mqtt_ok, lines=result.summary().splitlines(), error=result.mqtt_error)


# --------------------------------------------------------------------------- 渲染（纯函数）


def render_ports(probes: list[PortProbe]) -> list[str]:
    out: list[str] = []
    for probe in probes:
        if probe.ok:
            out.append(f"   {probe.port:>5} {probe.label}: 可连接 ✓")
        else:
            out.append(f"   {probe.port:>5} {probe.label}: 不可达 ✗（{probe.error}）")
    return out


def render_tls(probes: list[TlsProbe]) -> list[str]:
    out: list[str] = []
    for probe in probes:
        if probe.ok:
            chain = "已校验" if probe.verified else "未校验"
            out.append(f"   {probe.port:>5}: 握手成功 ✓ 证书链{chain}")
        else:
            out.append(f"   {probe.port:>5}: 握手失败 ✗ {probe.error}")
    return out


def render_camera(probe: CameraProbe) -> list[str]:
    if probe.ok:
        return [f"   成功 ✓ 取得 {probe.frame_bytes // 1024} KB 画面（{probe.seconds:.1f}s）"]
    return [f"   失败 ✗ 状态={probe.state} 说明={probe.detail}（{probe.seconds:.1f}s）"]


def render_rtsp_describe(probe: RtspDescribeProbe) -> list[str]:
    if not probe.connected:
        return [f"   ✗ TLS 连接失败：{probe.error}"]
    out = [f"   TLS 握手成功（证书链{'已校验' if probe.verified else '未校验'}）"]
    for path, head in probe.attempts:
        out.append(f"   DESCRIBE {path} -> {head}")
    out.extend(f"      {line}" for line in probe.sdp_lines)
    if probe.ok_path:
        out.append("   RTSP 服务正常，可以直接拉流 ✓")
    elif probe.unauthorized:
        out.append("   注：401/403 表示 RTSP 服务在运行，但用户名/口令未被接受")
    else:
        out.append("   注：无响应表示 RTSP 服务未开启（打印机上需打开「局域网实时画面」）")
    return out


def render_rtsp_frame(probe: RtspFrameProbe) -> list[str]:
    if not probe.available:
        return ["   跳过（未安装 opencv-python，该机型只能用 6000 端口）"]
    if probe.ok:
        return [
            f"   成功 ✓ 取得 {probe.frame_bytes // 1024} KB 帧（{probe.seconds:.1f}s），"
            f"路径 {probe.path}"
        ]
    return [f"   失败 ✗ 状态={probe.state} 说明={probe.detail}（{probe.seconds:.1f}s）"]


def render_mqtt(probe: MqttProbe) -> list[str]:
    return ["   " + line for line in probe.lines]


# --------------------------------------------------------------------------- 流程编排


def run(
    host: str,
    access_code: str,
    serial: str = "",
    *,
    with_rtsp_frame: bool = False,
    should_stop: Optional[Callable[[], bool]] = None,
    mqtt_timeout: float = PROBE_MQTT_TIMEOUT,
    on_step: Optional[Callable[[str], None]] = None,
) -> Iterator[SectionEvent]:
    """依次跑各步骤，产出 :class:`SectionStart` / :class:`SectionLines` 事件。

    一步一步产出而不是一次返回，是为了让界面能**边跑边显示**：一轮诊断要
    十几秒（三个端口各一次 TLS + 12 秒取帧 + 10 秒遥测），攒到最后再显示
    只会让人以为卡住了。先发 ``SectionStart`` 再干活，界面就能把标题先打出来。

    ``should_stop`` 在每步之前与耗时等待内部生效（遥测与取帧会把它透传下去），
    用户关掉对话框时最坏只多等一个步骤。
    """
    stopped = should_stop or (lambda: False)
    counter = 0

    def start(title: str) -> SectionStart:
        nonlocal counter
        prefix = _NUMBERS[counter] + " " if counter < len(_NUMBERS) else ""
        counter += 1
        return SectionStart(prefix + title)

    if not stopped():
        yield start(SECTION_PORTS)
        yield SectionLines(render_ports(check_ports(host)))
    if not stopped():
        yield start(SECTION_TLS)
        yield SectionLines(render_tls(check_tls(host)))
    if not stopped():
        yield start(SECTION_CAMERA)
        yield SectionLines(
            render_camera(
                grab_camera_frame(host, access_code, serial=serial, should_stop=should_stop)
            )
        )
    if not stopped():
        yield start(SECTION_RTSP_DESCRIBE)
        yield SectionLines(render_rtsp_describe(describe_rtsp(host, access_code)))
    if with_rtsp_frame and not stopped():
        yield start(SECTION_RTSP_FRAME)
        yield SectionLines(render_rtsp_frame(grab_rtsp_frame(host, access_code)))
    if not stopped():
        yield start(SECTION_MQTT)
        yield SectionLines(
            render_mqtt(
                probe_mqtt(
                    host,
                    access_code,
                    serial=serial,
                    timeout=mqtt_timeout,
                    on_step=on_step,
                    should_stop=should_stop,
                )
            )
        )
