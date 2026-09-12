"""假 Moonraker 服务器（仅供测试与演示）。

在没有真机的情况下端到端验证 `MoonrakerAdapter`：起一个本地 HTTP 服务，
按官方 Moonraker 的响应形态回数据，然后让适配器真的用 HTTP 去拉。
这样适配器的**传输层**（URL、方法、请求体、鉴权头、错误处理）也被覆盖，
而不是只测一个纯函数。

刻意用标准库 `http.server` 实现：
* 测试里可以直接 `pip` 无关地跑起来，不需要任何额外依赖；
* 与真实设备的差异都集中在本文件，便于对照官方文档核对。

支持的端点（对应 `app/adapters/moonraker/adapter.py` 里用到的）：

===============================  ==========================================
``GET  /server/info``            版本与 klippy 状态
``POST /printer/objects/query``  状态查询（本文件按请求体只回被问到的对象）
``GET  /server/webcams/list``    摄像头列表（可配置为空）
``GET  /server/files/metadata``  文件元数据（estimated_time 等）
``POST /printer/print/pause``    暂停
``POST /printer/print/resume``   继续
``POST /printer/print/cancel``   取消
``GET  /camera/monitor.jpg``     摄像头快照（JPEG）
===============================  ==========================================

**U1 的行为也被模拟**：``camera_requires_monitor`` 为真时，快照接口在收到
``GET /camera/start_monitor`` 之前返回 404（对应真实机器"不保活画面就静止"）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .ws import OP_TEXT, WebSocketError, encode_frame, read_frame

#: WebSocket 握手用的固定 GUID（RFC 6455）
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

#: 一个最小的合法 JPEG（1×1 灰点）。首字节必须是 FFD8 才能被适配器接受。
TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300"
    "080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20"
    "242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b0800"
    "010001011100ffc4001f0000010501010101010100000000000000000102030405060708"
    "090a0bffc400b5100002010303020403050504040000017d01020300041105122131"
    "410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25"
    "262728292a3435363738393a434445464748494a535455565758595a63646566676869"
    "6a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aa"
    "b2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7"
    "e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbfeffd9"
)


class FakeMoonraker:
    """一个可配置的假 Moonraker 实例。

    用法::

        with FakeMoonraker() as fake:
            adapter = MoonrakerAdapter(info, base_url=fake.base_url)
            adapter.start()
            ...
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        state: str = "printing",
        progress: float = 0.42,
        filename: str = "benchy.gcode",
        layer: Optional[int] = 12,
        total_layer: Optional[int] = 240,
        nozzle: float = 245.3,
        nozzle_target: float = 250.0,
        bed: float = 60.0,
        bed_target: float = 60.0,
        klippy_state: str = "ready",
        webhooks_state: str = "ready",
        camera: str = "snapshot",
        camera_requires_monitor: bool = False,
        api_key: str = "",
        estimated_time: Optional[float] = 3600.0,
    ) -> None:
        self.host = host
        self.port = port
        self.state = state
        self.progress = progress
        self.filename = filename
        self.layer = layer
        self.total_layer = total_layer
        self.nozzle = nozzle
        self.nozzle_target = nozzle_target
        self.bed = bed
        self.bed_target = bed_target
        self.klippy_state = klippy_state
        self.webhooks_state = webhooks_state
        #: "snapshot" 提供快照端点；"none" 表示没有摄像头；"list" 只通过 webcams/list 暴露
        self.camera = camera
        self.camera_requires_monitor = camera_requires_monitor
        self.api_key = api_key
        self.estimated_time = estimated_time
        # --- 观测点：测试据此断言"适配器到底请求了什么" ---
        self.requests: list[tuple[str, str]] = []
        self.commands: list[str] = []
        self.monitor_started = 0
        self.keepalive_seen = 0
        #: WebSocket 调用记录（方法名 + 参数）
        self.ws_calls: list[tuple[str, dict]] = []
        #: 受 WS 控制影响的状态
        self.led: Optional[bool] = None
        self.speed_factor: Optional[int] = None
        self.emergency_stopped = False
        #: 快照是否"新鲜"（模拟 U1：不保活就一直给陈旧帧）
        self.snapshot_fresh = not camera_requires_monitor

        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._ws: Optional[FakeMoonrakerWebSocket] = None

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> "FakeMoonraker":
        handler = _make_handler(self)

        class _Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):  # noqa: ANN001, ARG002
                # 客户端（适配器）在轮询间隙关闭连接是正常行为，不要刷栈
                pass

        self._httpd = _Server((self.host, self.port), handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="fake-moonraker", daemon=True
        )
        self._thread.start()
        # WebSocket 用独立端口：真实 Moonraker 与 HTTP 同端口（先握手再升级），
        # 这里简化成独立监听，测试通过 ws_url 指过来即可
        self._ws = FakeMoonrakerWebSocket(self).start()
        return self

    def stop(self) -> None:
        if getattr(self, "_ws", None) is not None:
            self._ws.stop()
            self._ws = None
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def handle_ws_call(self, method: str, params: dict) -> dict:
        """处理一条 WebSocket JSON-RPC 调用，返回 JSON-RPC 响应体。

        覆盖本项目用到的三类 WS-only 接口（官方源码确认它们 HTTP 发不通）：
        * ``camera.start_monitor`` —— U1 的摄像头保活
        * ``printer.control.led`` —— 舱灯（参数名是 ``name``）
        * ``printer.emergency_stop`` —— 急停
        """
        self.ws_calls.append((method, params))
        if method == "camera.start_monitor":
            self.monitor_started += 1
            self.keepalive_seen += 1
            # 保活后快照才开始更新（对应真机"不保活画面就静止"）
            self.snapshot_fresh = True
            return {"result": "ok"}
        if method == "printer.control.led":
            name = params.get("name")
            if not name:
                # 真实 Moonraker 的 _control_led 会因缺必填项报错
                return {"error": {"code": -32602, "message": "Missing required argument 'name'"}}
            self.led = bool(params.get("white"))
            return {"result": "ok"}
        if method == "printer.control.print_speed":
            percentage = params.get("percentage")
            if percentage is None:
                return {"error": {"code": -32602, "message": "Missing 'percentage'"}}
            self.speed_factor = int(percentage)
            return {"result": "ok"}
        if method == "printer.emergency_stop":
            self.emergency_stopped = True
            self.state = "error"
            return {"result": "ok"}
        return {"error": {"code": -32601, "message": f"Unknown method {method}"}}

    def __enter__(self) -> "FakeMoonraker":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def snapshot_url(self) -> str:
        return f"{self.base_url}/camera/monitor.jpg"

    # ------------------------------------------------------------------ 响应构造
    def objects_response(self, requested: dict[str, Any]) -> dict[str, Any]:
        """按请求体只回被问到的对象（真实 Moonraker 也是这个语义）。

        这个"按需返回"的特性正是适配器必须做增量合并的原因：一次响应里
        可能只有部分字段，整体覆盖会把其它读数清零。
        """
        everything: dict[str, Any] = {
            "print_stats": {
                "state": self.state,
                "filename": self.filename,
                "print_duration": 1234.5,
                "info": {},
            },
            "virtual_sdcard": {"progress": self.progress, "is_active": True},
            "display_status": {"progress": self.progress, "message": ""},
            "extruder": {"temperature": self.nozzle, "target": self.nozzle_target},
            "heater_bed": {"temperature": self.bed, "target": self.bed_target},
            "webhooks": {"state": self.webhooks_state},
            "toolhead": {"position": [10.0, 20.0, 5.0, 0.0]},
            "gcode_move": {"speed_factor": 1.0},
        }
        if self.layer is not None and self.total_layer is not None:
            everything["print_stats"]["info"] = {
                "current_layer": self.layer,
                "total_layer": self.total_layer,
            }
        status = {name: everything.get(name) for name in requested if name in everything}
        return {"result": {"status": status, "eventtime": time.time()}}

    def webcams_response(self) -> dict[str, Any]:
        webcams: list[dict[str, Any]] = []
        if self.camera in ("snapshot", "list"):
            webcams.append(
                {
                    "name": "camera",
                    "snapshot_url": "/camera/monitor.jpg",
                    "stream_url": "/camera/stream.mjpg",
                }
            )
        return {"result": {"webcams": webcams}}

    def metadata_response(self) -> dict[str, Any]:
        result: dict[str, Any] = {"filename": self.filename}
        if self.estimated_time is not None:
            result["estimated_time"] = self.estimated_time
        if self.total_layer is not None:
            result["layer_count"] = self.total_layer
        return {"result": result}


def _make_handler(fake: FakeMoonraker):
    class _Handler(BaseHTTPRequestHandler):
        server_version = "FakeMoonraker/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:  # noqa: A003 - 静音，避免污染测试输出
            pass

        # -------------------------------------------------------------- 工具
        def _send(self, payload: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def _send_json(self, data: dict, status: int = 200) -> None:
            self._send(
                json.dumps(data).encode("utf-8"), "application/json; charset=utf-8", status
            )

        def _authorized(self) -> bool:
            if not fake.api_key:
                return True
            return self.headers.get("X-Api-Key", "") == fake.api_key

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = 0
            if not length:
                return {}
            try:
                parsed = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}
            return parsed if isinstance(parsed, dict) else {}

        # -------------------------------------------------------------- 路由
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            fake.requests.append(("GET", path))
            if not self._authorized():
                self._send_json({"error": "unauthorized"}, 401)
                return
            if path == "/server/info":
                self._send_json(
                    {
                        "result": {
                            "moonraker_version": "1.4.1-fake",
                            "klippy_state": fake.klippy_state,
                            "components": ["klippy_apis", "webcam"],
                        }
                    }
                )
            elif path == "/server/webcams/list":
                self._send_json(fake.webcams_response())
            elif path == "/server/files/metadata":
                self._send_json(fake.metadata_response())
            elif path == "/camera/start_monitor":
                fake.monitor_started += 1
                fake.keepalive_seen += 1
                self._send_json({"result": "ok"})
            elif path == "/camera/monitor.jpg":
                if fake.camera == "none":
                    self._send(b"not found", "text/plain", 404)
                elif fake.camera_requires_monitor and not fake.snapshot_fresh:
                    # 模拟 U1：不周期性保活，画面就一直陈旧（真机实测行为）
                    self._send(b"stale frame", "text/plain", 404)
                else:
                    self._send(TINY_JPEG, "image/jpeg")
            else:
                self._send(b"not found", "text/plain", 404)

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            fake.requests.append(("POST", path))
            if not self._authorized():
                self._send_json({"error": "unauthorized"}, 401)
                return
            if path == "/printer/objects/query":
                body = self._body()
                objects = body.get("objects")
                if not isinstance(objects, dict):
                    objects = {}
                self._send_json(fake.objects_response(objects))
                return
            if path in (
                "/printer/print/pause",
                "/printer/print/resume",
                "/printer/print/cancel",
            ):
                action = path.rsplit("/", 1)[-1]
                fake.commands.append(action)
                if action == "pause":
                    fake.state = "paused"
                elif action == "resume":
                    fake.state = "printing"
                elif action == "cancel":
                    fake.state = "cancelled"
                self._send_json({"result": "ok"})
                return
            self._send(b"not found", "text/plain", 404)

    return _Handler


class FakeMoonrakerWebSocket:
    """假 Moonraker 的 WebSocket 端点，用于验证 WS-only 接口。

    真实 Moonraker 有三类接口只能走 WebSocket（官方源码里带
    ``transports = all & ~HTTP``）：急停、``/printer/control/*``、
    以及 U1 的摄像头保活 ``camera.start_monitor``。
    这个假端点把它们的语义补齐，让适配器的 WS 路径也能被离线测试覆盖。

    只需支持"收一条 JSON-RPC 请求、回一条响应"——足够窄，也足够真
    （真的会做握手与掩码校验，所以帧编解码写错会立刻暴露）。
    """

    def __init__(self, fake: FakeMoonraker) -> None:
        self.fake = fake
        self.port = 0
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> "FakeMoonrakerWebSocket":
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.fake.host, 0))
        self.port = server.getsockname()[1]
        server.listen(4)
        server.settimeout(0.3)
        self._server = server
        self._thread = threading.Thread(target=self._serve, name="fake-moonraker-ws", daemon=True)
        self._thread.start()
        return self

    @property
    def url(self) -> str:
        return f"ws://{self.fake.host}:{self.port}/websocket"

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _serve(self) -> None:
        while not self._stop.is_set():
            server = self._server
            if server is None:
                return
            try:
                conn, _addr = server.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            threading.Thread(
                target=self._handle, args=(conn,), name="fake-moonraker-ws-conn", daemon=True
            ).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5.0)
            if not self._handshake(conn):
                return
            while not self._stop.is_set():
                try:
                    opcode, payload = read_frame(conn, expect_masked=True)
                except (WebSocketError, OSError):
                    return
                if opcode != OP_TEXT:
                    continue
                try:
                    request = json.loads(payload.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(request, dict):
                    continue
                method = str(request.get("method", ""))
                params = request.get("params")
                response_body = self.fake.handle_ws_call(
                    method, params if isinstance(params, dict) else {}
                )
                response_body["jsonrpc"] = "2.0"
                response_body["id"] = request.get("id")
                conn.sendall(
                    encode_frame(OP_TEXT, json.dumps(response_body).encode("utf-8"), mask=False)
                )
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handshake(self, conn: socket.socket) -> bool:
        """完成 RFC 6455 握手；顺带校验 Accept key，帧层写错能立刻发现。"""
        header = bytearray()
        while b"\r\n\r\n" not in header:
            try:
                chunk = conn.recv(1)
            except OSError:
                return False
            if not chunk:
                return False
            header += chunk
            if len(header) > 16384:
                return False
        text = header.decode("latin-1")
        request_line = text.split("\r\n", 1)[0]
        # 只接受 /websocket（真实 Moonraker 用这个路径）
        if "GET /websocket" not in request_line:
            conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            return False
        key = ""
        for line in text.split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        if not key:
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            return False
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
        )
        return True
