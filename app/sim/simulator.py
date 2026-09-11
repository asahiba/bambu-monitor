"""拓竹打印机模拟器。

在没有真机的情况下验证整条链路：

* UDP 2021 广播响应 + SSDP(1990) 响应 —— 用于测试「自动搜索」
* 8883 MQTT over TLS 极简 Broker —— 推送进度 / 喷嘴温度 / 热床温度
* 6000 端口 TLS + 80 字节鉴权包 + JPEG 帧 —— 用于测试实时画面

用法::

    python -m app.sim --count 4          # 启动 4 台虚拟打印机
    python -m app --sim                  # 同时启动软件并连接这些虚拟打印机
"""

from __future__ import annotations

import argparse
import json
import os
import random
import socket
import ssl
import struct
import sys
import threading
import time
from typing import Optional

SIM_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".sim")
DEFAULT_ACCESS_CODE = "12345678"

SIM_MODELS = [
    # 序列号必须是 15 位大写字母数字（真机格式，见 discovery._looks_like_bambu）：
    # 否则「没有 devmodel 头、仅凭序列号识别」这条发现路径在模拟器上永远走不通。
    ("01P00A123456789", "P1S", "车间 P1S"),
    ("03900A234567890", "A1", "车间 A1"),
    ("03000A345678901", "A1 mini", "桌上 A1 mini"),
    ("00M00A456789012", "X1C", "车间 X1C"),
    ("09400A567890123", "H2D", "打样 H2D"),
    ("22E00A678901234", "P2S", "二号 P2S"),
]


# --------------------------------------------------------------------------- 证书
def ensure_self_signed_cert(serial: str) -> tuple[str, str]:
    """生成模拟器用的自签证书（CN=序列号，与真机结构一致）。"""
    os.makedirs(SIM_DIR, exist_ok=True)
    cert_path = os.path.join(SIM_DIR, "cert.pem")
    key_path = os.path.join(SIM_DIR, "key.pem")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path
    try:
        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("模拟器需要 cryptography：pip install cryptography") from exc

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, serial)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    with open(key_path, "wb") as handle:
        handle.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    with open(cert_path, "wb") as handle:
        handle.write(certificate.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def make_server_context() -> ssl.SSLContext:
    cert_path, key_path = ensure_self_signed_cert("SIMULATOR")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    try:
        context.maximum_version = ssl.TLSVersion.TLSv1_2
    except (ValueError, AttributeError):  # pragma: no cover
        pass
    return context


_qt_app = None


def ensure_qt_initialized() -> None:
    """绘制模拟画面需要字体数据库，因此必须先有 QGuiApplication。

    在软件界面里运行时已经存在 QApplication；在 --core-test 等无界面场景下
    这里会用 offscreen 平台补一个，避免 Qt 因缺少应用实例直接终止进程。
    """
    global _qt_app
    try:
        from PySide6.QtGui import QGuiApplication
    except ImportError:
        return
    if QGuiApplication.instance() is not None:
        return
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _qt_app = QGuiApplication([])


# --------------------------------------------------------------------------- 画面
class FrameFactory:
    """生成 1280x720 的模拟打印画面（JPEG）。

    优先用 Qt 绘制（桌面版自带）；在没有 Qt 的环境（Linux / Docker 无界面模式）
    自动退回 OpenCV 绘制，保证模拟器在容器里也能出画面。
    """

    def __init__(self, serial: str, name: str, hue: int) -> None:
        self.serial = serial
        self.name = name
        self.hue = hue
        self._counter = 0
        self._qt_ok: Optional[bool] = None

    def render(self) -> bytes:
        if self._qt_ok is None:
            # 只在真正需要绘制时探测一次 Qt 是否可用。
            # 注意：find_spec("PySide6.QtGui") 在父包不存在时会抛异常而不是返回 None，
            # 所以必须整段包在 try 里。
            try:
                import importlib.util

                self._qt_ok = importlib.util.find_spec("PySide6.QtGui") is not None
            except Exception:  # noqa: BLE001
                self._qt_ok = False
        if self._qt_ok:
            try:
                return self._render_qt()
            except Exception:  # noqa: BLE001
                self._qt_ok = False
        return self._render_cv2()

    def _render_qt(self) -> bytes:
        from PySide6.QtCore import QBuffer, QByteArray, QRect, Qt
        from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter

        width, height = 1280, 720
        image = QImage(width, height, QImage.Format_RGB32)
        painter = QPainter(image)
        try:
            gradient = QLinearGradient(0, 0, 0, height)
            gradient.setColorAt(0.0, QColor.fromHsv(self.hue, 60, 40))
            gradient.setColorAt(1.0, QColor.fromHsv(self.hue, 90, 18))
            painter.fillRect(0, 0, width, height, gradient)

            # 模拟热床和正在打印的模型
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(30, 34, 38))
            painter.drawRect(120, 470, 1040, 40)
            painter.setBrush(QColor(60, 66, 72))
            painter.drawRect(150, 300, 260, 170)
            painter.setBrush(QColor.fromHsv(self.hue, 160, 220))
            self._counter = (self._counter + 7) % 150
            painter.drawRect(560, 470 - self._counter * 2, 160, self._counter * 2)
            painter.setBrush(QColor(210, 214, 218))
            painter.drawRect(560, 250, 160, 30)

            painter.setPen(QColor(230, 240, 245))
            painter.setFont(QFont("Consolas", 26))
            painter.drawText(QRect(0, 20, width, 40), Qt.AlignHCenter, f"SIMULATED CAMERA · {self.serial}")
            painter.setFont(QFont("Microsoft YaHei UI", 20))
            painter.drawText(QRect(0, 620, width, 40), Qt.AlignHCenter, f"{self.name} · {time.strftime('%H:%M:%S')}")
        finally:
            painter.end()

        byte_array = QByteArray()
        buffer = QBuffer(byte_array)
        buffer.open(QBuffer.WriteOnly)
        image.save(buffer, "JPG", 80)
        buffer.close()
        return bytes(byte_array.data())

    def _render_cv2(self) -> bytes:
        """无 Qt 环境下的绘制（Linux / Docker 无界面模式）。"""
        import colorsys

        import cv2
        import numpy as np

        def bgr(hue: float, saturation: float, value: float) -> tuple[int, int, int]:
            red, green, blue = colorsys.hsv_to_rgb(hue % 1.0, saturation, value)
            return int(blue * 255), int(green * 255), int(red * 255)

        width, height = 1280, 720
        hue = self.hue / 359.0
        top = np.array(bgr(hue, 0.35, 0.16), dtype=np.float32)
        bottom = np.array(bgr(hue, 0.35, 0.07), dtype=np.float32)
        ramp = np.linspace(0.0, 1.0, height, dtype=np.float32).reshape(height, 1, 1)
        frame = top.reshape(1, 1, 3) * (1 - ramp) + bottom.reshape(1, 1, 3) * ramp
        frame = np.repeat(frame, width, axis=1).astype(np.uint8)

        cv2.rectangle(frame, (120, 470), (1160, 510), (38, 34, 30), -1)
        cv2.rectangle(frame, (150, 300), (410, 470), (72, 66, 60), -1)
        self._counter = (self._counter + 7) % 150
        cv2.rectangle(frame, (560, 470 - max(4, self._counter * 2)), (720, 470),
                      bgr(hue, 0.7, 0.86), -1)
        cv2.rectangle(frame, (560, 250), (720, 280), (218, 214, 210), -1)
        cv2.putText(frame, f"SIMULATED CAMERA / {self.serial}", (330, 55),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.1, (245, 240, 230), 2, cv2.LINE_AA)
        cv2.putText(frame, f"{self.name}  {time.strftime('%H:%M:%S')}", (470, 660),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (240, 240, 240), 2, cv2.LINE_AA)

        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return buffer.tobytes() if ok else b""


# --------------------------------------------------------------------------- MQTT
def _read_varint(sock) -> Optional[int]:
    multiplier = 1
    value = 0
    for _ in range(4):
        chunk = sock.recv(1)
        if not chunk:
            return None
        byte = chunk[0]
        value += (byte & 0x7F) * multiplier
        if not byte & 0x80:
            return value
        multiplier *= 128
    return None


def _read_packet(sock) -> Optional[tuple[int, bytes]]:
    head = sock.recv(1)
    if not head:
        return None
    remaining = _read_varint(sock)
    if remaining is None:
        return None
    body = b""
    while len(body) < remaining:
        chunk = sock.recv(remaining - len(body))
        if not chunk:
            return None
        body += chunk
    return head[0], body


def _encode_remaining_length(length: int) -> bytes:
    out = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length:
            byte |= 0x80
        out.append(byte)
        if not length:
            return bytes(out)


class MiniMqttBroker(threading.Thread):
    """只实现监控软件需要的那部分 MQTT 3.1.1 服务端。"""

    def __init__(self, printer: "FakePrinter") -> None:
        super().__init__(name=f"sim-mqtt-{printer.serial}", daemon=True)
        self.printer = printer
        self._stop = threading.Event()
        self._clients: list[ssl.SSLSocket] = []
        self._lock = threading.Lock()
        self._server: Optional[socket.socket] = None
        self.context = make_server_context()

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        with self._lock:
            for client in self._clients:
                try:
                    client.close()
                except OSError:
                    pass
            self._clients.clear()

    def run(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind((self.printer.ip, 8883))
            server.listen(4)
            server.settimeout(0.5)
        except OSError as exc:
            print(f"[sim] MQTT 端口绑定失败 {self.printer.ip}:8883 -> {exc}")
            return
        self._server = server
        while not self._stop.is_set():
            try:
                conn, _ = server.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            tls = self.context.wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError):
            try:
                conn.close()
            except OSError:
                pass
            return
        tls.settimeout(1.0)
        with self._lock:
            self._clients.append(tls)
        try:
            while not self._stop.is_set():
                try:
                    packet = _read_packet(tls)
                except (socket.timeout, TimeoutError, ssl.SSLWantReadError):
                    continue
                except (OSError, ssl.SSLError):
                    break
                if packet is None:
                    break
                first, body = packet
                packet_type = first >> 4
                if packet_type == 1:  # CONNECT
                    tls.sendall(bytes([0x20, 0x02, 0x00, 0x00]))
                    # 连接后立即推一次完整状态
                    self._send_status(tls)
                elif packet_type == 8:  # SUBSCRIBE
                    pid = body[0:2]
                    tls.sendall(bytes([0x90, 0x03]) + pid + bytes([0x00]))
                    self._send_status(tls)
                elif packet_type == 3:  # PUBLISH（客户端下发的控制命令）
                    self._handle_publish(body)
                elif packet_type == 12:  # PINGREQ
                    tls.sendall(bytes([0xD0, 0x00]))
                elif packet_type == 14:  # DISCONNECT
                    break
        finally:
            with self._lock:
                if tls in self._clients:
                    self._clients.remove(tls)
            try:
                tls.close()
            except OSError:
                pass

    def _send_status(self, tls: ssl.SSLSocket) -> None:
        topic = f"device/{self.printer.serial}/report".encode()
        payload = self.printer.status_report()
        body = struct.pack(">H", len(topic)) + topic + payload
        packet = bytes([0x30]) + _encode_remaining_length(len(body)) + body
        try:
            tls.sendall(packet)
        except (OSError, ssl.SSLError):
            pass

    def _handle_publish(self, body: bytes) -> None:
        """解析客户端发来的 PUBLISH：主题长度(2) + 主题 + 负载。"""
        if len(body) < 2:
            return
        topic_length = struct.unpack_from(">H", body, 0)[0]
        if len(body) < 2 + topic_length:
            return
        topic = body[2 : 2 + topic_length].decode("utf-8", "replace")
        payload = body[2 + topic_length :]
        if not topic.endswith("/request"):
            return
        try:
            data = json.loads(payload.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if isinstance(data, dict):
            if "pushing" in data:
                # pushall：立刻回一份完整状态
                return
            self.printer.handle_command(data)

    def broadcast(self) -> None:
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            self._send_status(client)


# --------------------------------------------------------------------------- 打印机
class FakePrinter:
    """一台虚拟打印机（MQTT + 摄像头）。"""

    def __init__(self, ip: str, serial: str, model: str, name: str, access_code: str, seed: int) -> None:
        self.ip = ip
        self.serial = serial
        self.model = model
        self.name = name
        self.access_code = access_code
        self.firmware = "01.05.00.00"
        self.random = random.Random(seed)
        self.percent = self.random.randint(5, 80)
        self.remaining = self.random.randint(20, 300)
        self.layer = 12
        self.total_layers = 240
        self.nozzle_target = self.random.choice([220.0, 240.0, 250.0])
        self.bed_target = self.random.choice([55.0, 60.0, 65.0])
        self.started = time.time()
        self.frame_factory = FrameFactory(serial, name, self.random.randint(0, 359))
        self.mqtt = MiniMqttBroker(self)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        # 可控状态（用于验证暂停/继续/停止/灯光指令）
        self.paused = False
        self.stopped = False
        self.light_on = True
        self.speed_level = 2
        self.chamber_temper = round(self.random.uniform(28.0, 38.0), 1)
        self.wifi_signal = f"-{self.random.randint(40, 70)}dBm"
        self.received_commands: list[dict] = []
        self.current_tray = self.random.choice([0, 1, 2, 3])
        # 最后一台模拟机默认带一条 HMS 提示，方便验证 HMS 显示
        self.hms: list[dict] = []
        self.ams_trays = self._build_trays()

    # -------------------------------------------------------------- 耗材
    def _build_trays(self) -> list[dict]:
        """4 个槽位：3 个官方料卷（带 RFID，有真实余量）+ 1 个第三方料卷（无 RFID，不显示余量）。"""
        palette = [
            ("PLA", "FF3B30FF", True),
            ("PETG", "34C759FF", True),
            ("ABS", "0A84FFFF", True),
            ("PLA", "FFCC00FF", False),  # 第三方料卷：读不到余量
        ]
        trays = []
        for tray_id, (tray_type, color, has_rfid) in enumerate(palette):
            trays.append(
                {
                    "id": str(tray_id),
                    "tray_type": tray_type,
                    "tray_color": color,
                    "remain": self.random.randint(15, 95),
                    "nozzle_temp_min": "190" if tray_type.startswith("PLA") else "240",
                    "nozzle_temp_max": "240" if tray_type.startswith("PLA") else "270",
                    "tag_uid": f"A1B2C3D4E5F6{tray_id:04X}" if has_rfid else "0000000000000000",
                    "tray_uuid": (f"00000000{tray_id:04X}0000000000000000" if has_rfid else "0" * 32),
                    "tray_info_idx": "GFA00" if has_rfid else "GFU00",
                }
            )
        return trays

    # -------------------------------------------------------------- 状态
    def status_report(self) -> bytes:
        elapsed = time.time() - self.started
        nozzle = min(self.nozzle_target, 25.0 + elapsed * 30) + self.random.uniform(-0.4, 0.4)
        bed = min(self.bed_target, 22.0 + elapsed * 12) + self.random.uniform(-0.2, 0.2)
        if self.stopped:
            state = "IDLE"
        elif self.paused:
            state = "PAUSE"
        else:
            state = "RUNNING"
        payload = {
            "print": {
                "command": "push_status",
                "gcode_state": state,
                "mc_percent": self.percent,
                "mc_remaining_time": self.remaining,
                "layer_num": self.layer,
                "total_layer_num": self.total_layers,
                "subtask_name": f"{self.name.replace(' ', '_')}.gcode.3mf",
                "nozzle_temper": round(nozzle, 1),
                "nozzle_target_temper": self.nozzle_target,
                "bed_temper": round(bed, 1),
                "bed_target_temper": self.bed_target,
                "chamber_temper": self.chamber_temper,
                "wifi_signal": self.wifi_signal,
                "print_error": 0,
                "hms": self.hms,
                "lights_report": [
                    {"node": "chamber_light", "mode": "on" if self.light_on else "off"},
                    {"node": "work_light", "mode": "flashing"},
                ],
                "ams": {
                    "ams": [
                        {
                            "id": "0",
                            "humidity": "4",
                            "temp": "26.5",
                            "tray": self.ams_trays,
                        }
                    ],
                    "ams_exist_bits": "1",
                    "tray_now": str(self.current_tray),
                    "tray_tar": "0",
                    "version": 4,
                },
                "vt_tray": {
                    "id": "254",
                    "tray_type": "PLA",
                    "tray_color": "EDEDEDFF",
                    "remain": 42,
                    "nozzle_temp_min": "190",
                    "nozzle_temp_max": "240",
                    "tag_uid": "0000000000000000",  # 外挂料盘没有余量检测
                },
                "spd_lvl": self.speed_level,
                "ipcam": {
                    "ipcam_dev": "1",
                    "resolution": "1080p",
                    "rtsp_url": f"rtsps://{self.ip}:322/streaming/live/1",
                },
            }
        }
        return json.dumps(payload).encode()

    # -------------------------------------------------------------- 指令
    def handle_command(self, payload: dict) -> None:
        """处理监控软件下发的控制指令（模拟器只改状态并记录，便于自动化验证）。"""
        self.received_commands.append(payload)
        if len(self.received_commands) > 50:
            del self.received_commands[:-50]
        for section, body in payload.items():
            if not isinstance(body, dict):
                continue
            command = str(body.get("command", ""))
            if section == "print":
                if command == "pause":
                    self.paused = True
                elif command == "resume":
                    self.paused = False
                elif command == "stop":
                    self.stopped = True
                    self.paused = False
                    self.percent = 0
                elif command == "print_speed":
                    self.speed_level = int(body.get("param", self.speed_level) or 2)
            elif section == "system" and command == "ledctrl":
                node = str(body.get("led_node", "chamber_light"))
                if node.startswith("chamber_light"):
                    self.light_on = str(body.get("led_mode", "on")) == "on"
        self.mqtt.broadcast()

    def _tick_status(self) -> None:
        while not self._stop.wait(2.0):
            if not self.paused and not self.stopped:
                self.percent = min(100, self.percent + 1)
                self.remaining = max(0, self.remaining - 1)
                self.layer = min(self.total_layers, self.layer + 1)
                if self.percent >= 100:
                    self.percent = 0
                    self.layer = 0
                    self.remaining = self.random.randint(180, 400)
            self.mqtt.broadcast()

    # -------------------------------------------------------------- 摄像头
    def _camera_loop(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind((self.ip, 6000))
            server.listen(4)
            server.settimeout(0.5)
        except OSError as exc:
            print(f"[sim] 摄像头端口绑定失败 {self.ip}:6000 -> {exc}")
            return
        context = make_server_context()
        while not self._stop.is_set():
            try:
                conn, _ = server.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            threading.Thread(target=self._serve_camera, args=(conn, context), daemon=True).start()
        try:
            server.close()
        except OSError:
            pass

    def _serve_camera(self, conn: socket.socket, context: ssl.SSLContext) -> None:
        try:
            tls = context.wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError):
            try:
                conn.close()
            except OSError:
                pass
            return
        tls.settimeout(1.0)
        try:
            auth = b""
            while len(auth) < 80:
                chunk = tls.recv(80 - len(auth))
                if not chunk:
                    return
                auth += chunk
            username = auth[16:48].rstrip(b"\x00").decode("ascii", "ignore")
            code = auth[48:80].rstrip(b"\x00").decode("ascii", "ignore")
            if username != "bblp" or code != self.access_code:
                print(f"[sim] {self.serial} 拒绝连接：访问代码不匹配（收到 {code!r}）", flush=True)
                return
            while not self._stop.is_set():
                try:
                    frame = self.frame_factory.render()
                except Exception as exc:  # noqa: BLE001 - 模拟器不能因为绘制失败而中断
                    print(f"[sim] 生成模拟画面失败：{exc!r}", flush=True)
                    time.sleep(1.0)
                    continue
                if not frame:
                    print("[sim] 生成模拟画面为空（JPEG 编码可能不可用）", flush=True)
                    time.sleep(1.0)
                    continue
                header = struct.pack("<IIII", len(frame), 0, 1, 0)
                tls.sendall(header + frame)
                time.sleep(0.6)
        except (OSError, ssl.SSLError):
            return
        finally:
            try:
                tls.close()
            except OSError:
                pass

    # -------------------------------------------------------------- 生命周期
    def start(self) -> None:
        self.mqtt.start()
        for target in (self._camera_loop, self._tick_status):
            thread = threading.Thread(target=target, name=f"sim-{self.serial}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        self.mqtt.stop()
        for thread in self._threads:
            thread.join(timeout=1.5)


# --------------------------------------------------------------------------- 发现服务
class DiscoveryResponder(threading.Thread):
    """统一响应 SSDP 与 2021 广播（所有虚拟打印机共用）。"""

    def __init__(self, printers: list[FakePrinter]) -> None:
        super().__init__(name="sim-discovery", daemon=True)
        self.printers = printers
        self._stop = threading.Event()
        self._sockets: list[socket.socket] = []

    def stop(self) -> None:
        self._stop.set()
        for sock in self._sockets:
            try:
                sock.close()
            except OSError:
                pass

    def run(self) -> None:
        ssdp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ssdp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        legacy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        legacy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            ssdp.bind(("", 1990))
            mreq = socket.inet_aton("239.255.255.250") + socket.inet_aton("0.0.0.0")
            ssdp.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as exc:
            print(f"[sim] SSDP 端口 1990 绑定失败：{exc}")
        try:
            legacy.bind(("", 2021))
        except OSError as exc:
            print(f"[sim] 旧版发现端口 2021 绑定失败：{exc}")
        for sock in (ssdp, legacy):
            sock.settimeout(0.5)
            self._sockets.append(sock)

        while not self._stop.is_set():
            for sock in (ssdp, legacy):
                try:
                    data, addr = sock.recvfrom(2048)
                except (socket.timeout, TimeoutError):
                    continue
                except OSError:
                    return
                if b"M-SEARCH" in data.upper():
                    for printer in self.printers:
                        reply = (
                            "HTTP/1.1 200 OK\r\n"
                            f"Location: {printer.ip}\r\n"
                            f"USN: {printer.serial}\r\n"
                            "Server: UPnP/1.0\r\n"
                            f"devmodel.bambu.com: {printer.model}\r\n"
                            f"devname.bambu.com: {printer.name}\r\n"
                            "devconnect.bambu.com: lan\r\n"
                            "devbind.bambu.com: free\r\n"
                            f"devversion.bambu.com: {printer.firmware}\r\n"
                            "\r\n"
                        ).encode()
                        try:
                            sock.sendto(reply, addr)
                        except OSError:
                            pass
                else:
                    for printer in self.printers:
                        payload = json.dumps(
                            {
                                "dev_ip": printer.ip,
                                "dev_id": printer.serial,
                                "dev_name": printer.name,
                                "dev_version": printer.firmware,
                                "dev_signal": "-50",
                                "dev_connect": "lan",
                            }
                        ).encode()
                        try:
                            sock.sendto(payload, addr)
                        except OSError:
                            pass


# --------------------------------------------------------------------------- 启动
def start_simulator(count: int = 4, access_code: str = DEFAULT_ACCESS_CODE) -> tuple[list[FakePrinter], DiscoveryResponder]:
    """在当前进程内启动若干虚拟打印机（数量可以超过内置机型数，会循环复用机型）。"""
    ensure_qt_initialized()
    printers: list[FakePrinter] = []
    for index in range(max(1, count)):
        base_serial, model, name = SIM_MODELS[index % len(SIM_MODELS)]
        serial = base_serial
        if index >= len(SIM_MODELS):
            serial = base_serial[:-3] + f"{index:03d}"
        printer_name = name if index < len(SIM_MODELS) else f"{name}-{index + 1}"
        info = FakePrinter(
            ip=f"127.0.0.{index + 2}",
            serial=serial,
            model=model,
            name=printer_name,
            access_code=access_code,
            seed=index * 17 + 3,
        )
        info.start()
        printers.append(info)
    # 最后一台带一条 HMS 提示，用来验证 HMS 显示的渲染与文案解析
    if printers:
        printers[-1].hms = [{"attr": 0x03000100, "code": 0x00010001}]
    responder = DiscoveryResponder(printers)
    responder.start()
    return printers, responder


def stop_simulator(printers: list[FakePrinter], responder: DiscoveryResponder) -> None:
    responder.stop()
    for printer in printers:
        printer.stop()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="拓竹打印机模拟器")
    parser.add_argument("--count", type=int, default=4, help="虚拟打印机数量（最多 6）")
    parser.add_argument("--code", default=DEFAULT_ACCESS_CODE, help="局域网访问代码")
    args = parser.parse_args(argv)

    printers, responder = start_simulator(args.count, args.code)
    print(f"[sim] 已启动 {len(printers)} 台虚拟打印机，访问代码：{args.code}")
    for printer in printers:
        print(f"      {printer.ip:<12} {printer.model:<8} {printer.serial}  {printer.name}")
    print("[sim] 在软件里点「自动搜索」即可发现；Ctrl+C 退出。")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[sim] 正在停止…")
    finally:
        stop_simulator(printers, responder)
    return 0


if __name__ == "__main__":
    sys.exit(main())
